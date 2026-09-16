#!/usr/bin/env python3
# [DISABLED=True] Removed from BACKTEST_SIGNALS and registry in Cycle 3.
# Redundant with NWP dT/dt fusion already in ensemble. Keeping code file.
# CHANGELOG (last 10 broad changes):
# 1. [File history unavailable - check git blame]
#


import sqlite3
import os
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional, Tuple, List, Dict
import logging
from .base_signal import BaseSignal

logger = logging.getLogger(__name__)

NWP_DB_DEFAULT = "data/nwp_forecasts.db"

# Models in order of preference
MODELS = ['GFS', 'ECMWF', 'ICON', 'GEM']


class NwpDirectSignal(BaseSignal):

    def __init__(self, db_path: str = None):
        if db_path and os.path.exists(db_path):
            self.nwp_db_path = Path(db_path).absolute()
        elif os.environ.get('NWP_DB_PATH'):
            self.nwp_db_path = Path(os.environ['NWP_DB_PATH']).absolute()
        else:
            self.nwp_db_path = Path(NWP_DB_DEFAULT).resolve()
            # Try relative to project root
            if not self.nwp_db_path.exists():
                alt = Path(__file__).resolve().parent.parent.parent / NWP_DB_DEFAULT
                if alt.exists():
                    self.nwp_db_path = alt

        self._cache = {}
        self._model_order = MODELS

    @property
    def name(self) -> str:
        return "nwp_direct"

    @property
    def min_lookback(self) -> int:
        return 0

    def evaluate(self, idx: int, days: list) -> Tuple[Optional[str], float]:
        """Standard evaluate interface - queries NWP forecasts for the given day."""
        # Get station context from instance attribute (set externally or by evaluate_for_station)
        station = getattr(self, '_station', None)
        if station is None:
            return None, 0.0

        # Get date from days list
        if idx < 0 or idx >= len(days):
            return None, 0.0
        target_date = days[idx].get('date') if isinstance(days[idx], dict) else None
        if target_date is None:
            return None, 0.0

        # Default to HIGH market type; evaluate_for_station will handle it
        return self.evaluate_for_station(station, str(target_date), market_type='HIGH')

    def evaluate_for_station(self, station: str, date: str,
                              market_type: str = 'HIGH',
                              conn: sqlite3.Connection = None) -> Tuple[Optional[str], float]:
        """Evaluate for a specific station+date+market_type using NWP forecasts.

        HIGH -> temperature_2m_max, LOW -> temperature_2m_min
        """
        field = 'temperature_2m_max' if market_type == 'HIGH' else 'temperature_2m_min'

        try:
            today_dt = datetime.strptime(date, '%Y-%m-%d')
        except ValueError:
            return None, 0.0
        tomorrow_date = (today_dt + timedelta(days=1)).strftime('%Y-%m-%d')

        # Collect votes from all available models
        votes = []  # list of model directions
        magnitudes = []

        for model in self._model_order:
            today_temp = self._get_temp(station, date, model, field)
            tomorrow_temp = self._get_temp(station, tomorrow_date, model, field)

            if today_temp is not None and tomorrow_temp is not None:
                mag = abs(tomorrow_temp - today_temp)
                magnitudes.append(mag)
                if tomorrow_temp > today_temp:
                    votes.append('up')
                elif tomorrow_temp < today_temp:
                    votes.append('down')
                # Equal -> no vote from this model

        if not votes:
            return None, 0.0

        # Count votes
        up = sum(1 for v in votes if v == 'up')
        down = sum(1 for v in votes if v == 'down')
        total = up + down

        if up == down:
            return None, 0.0

        direction = 'up' if up > down else 'down'

        # Confidence = magnitude-based tier
        avg_mag = sum(magnitudes) / len(magnitudes) if magnitudes else 0
        conf = self._magnitude_confidence(avg_mag, market_type)

        # Boost confidence for model agreement
        max_votes = max(up, down)
        agreement = max_votes / total if total > 0 else 0.5
        if agreement >= 0.8 and total >= 3:
            conf = min(0.98, conf + 0.03)
        elif agreement < 0.6 and total >= 2:
            conf = max(0.55, conf - 0.10)

        return direction, conf

    def _magnitude_confidence(self, magnitude_c: float, market_type: str = 'HIGH') -> float:
        """Return confidence based on forecast magnitude."""
        if magnitude_c < 0.5:
            return 0.70 if market_type == 'HIGH' else 0.65
        elif magnitude_c < 1.5:
            return 0.85
        elif magnitude_c < 5.0:
            return 0.95
        else:
            return 0.98

    def _get_temp(self, station: str, target_date: str, model: str,
                  field: str = 'temperature_2m_max') -> Optional[float]:
        """Get temperature from NWP DB."""
        key = (station, target_date, model, field)
        if key in self._cache:
            return self._cache[key]

        if not os.path.exists(str(self.nwp_db_path)):
            return None

        try:
            conn = sqlite3.connect(str(self.nwp_db_path), timeout=30)
            cur = conn.cursor()
            # Model names in DB are lowercase
            cur.execute("""
                SELECT AVG(value) FROM nwp_forecasts
                WHERE station = ? AND target_date = ? AND LOWER(model) = ? AND variable = ?
                AND value IS NOT NULL AND value > -100 AND value < 150
            """, (station, target_date, model.lower(), field))
            row = cur.fetchone()
            conn.close()
            if row and row[0] is not None:
                val = float(row[0])
                self._cache[key] = val
                return val
        except Exception as e:
            logger.debug(f"NWP lookup error {station} {target_date} {model} {field}: {e}")

        self._cache[key] = None
        return None

    def get_calibration_key(self) -> str:
        return self.name

    def compute_signal(self, station: str, target_date: str = None) -> Optional[Dict]:
        """Public dict-returning API (compatibility with NwpAnalogSignal callers)."""
        from datetime import datetime as dt
        if target_date is None:
            target_date = dt.now().strftime('%Y-%m-%d')
        direction, confidence = self.evaluate_for_station(station, target_date, market_type='HIGH')
        if direction is None:
            return None
        return {
            'direction': 1 if direction == 'up' else -1,
            'confidence': confidence,
            'source': 'nwp_direct',
        }


# ── Standalone test ──────────────────────────────────────────────────

STATIONS = ['KATL','KAUS','KBOS','KDCA','KDEN','KDFW','KHOU','KLAS',
            'KLAX','KMDW','KMIA','KMSP','KMSY','KNYC','KOKC','KPHL',
            'KPHX','KSAT','KSEA','KSFO']


def test_signal():
    """Quick test of the signal."""
    s = NwpDirectSignal()
    print(f"Signal: {s.name}")
    for station in ['KNYC', 'KATL', 'KDCA']:
        for date in ['2026-06-01', '2026-06-15', '2026-07-01']:
            d, c = s._evaluate_market(station, date, 'HIGH')
            print(f"  {station} {date} HIGH: {d} @ {c:.3f}")
    print("Done")


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    test_signal()