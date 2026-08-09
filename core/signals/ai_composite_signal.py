#!/usr/bin/env python3
# CHANGELOG (last 10 broad changes):
# 1. [2026-07-22 B-Mode 3: Open AI/ML gate — add AIGFS, GraphCast, AIFS]
#

"""
AI Composite Signal

Blends AI model predictions (AIGFS, GraphCast, AIFS) with classical NWP
forecasts. Reports AI-vs-classical divergence as a confidence modulator.

AI models produce 6-hourly timestamps (0, 6, 12, 18 UTC). Classical NWP
models produce daily min/max aggregates. The signal compares the daily
temperature direction predicted by each family and adjusts confidence
based on inter-family agreement.

When the AI and classical model families agree on direction, confidence
is boosted. When they diverge, confidence is reduced proportionally to
the divergence magnitude.

Data source: ai_forecasts table (AI models) + nwp_forecasts table (classical)
"""

import sqlite3
import os
import logging
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional, Tuple, List, Dict
from .base_signal import BaseSignal

logger = logging.getLogger(__name__)

NWP_DB_DEFAULT = "data/nwp_forecasts.db"

# AI models queried from ai_forecasts (in order of preference)
AI_MODELS = ['aigfs', 'graphcast', 'aifs']

# Classical NWP models queried from nwp_forecasts
CLASSICAL_MODELS = ['gfs', 'ecmwf', 'icon', 'gem']

# Temperature fields to compare
AI_TEMP_FIELD = 'temp_f'
CLASSICAL_TEMP_FIELD_MAX = 'temperature_2m_max'
CLASSICAL_TEMP_FIELD_MIN = 'temperature_2m_min'


class AiCompositeSignal(BaseSignal):

    def __init__(self, db_path: str = None):
        if db_path and os.path.exists(db_path):
            self.nwp_db_path = Path(db_path).absolute()
        elif os.environ.get('NWP_DB_PATH'):
            self.nwp_db_path = Path(os.environ['NWP_DB_PATH']).absolute()
        else:
            self.nwp_db_path = Path(NWP_DB_DEFAULT).resolve()
            if not self.nwp_db_path.exists():
                alt = Path(__file__).resolve().parent.parent.parent / NWP_DB_DEFAULT
                if alt.exists():
                    self.nwp_db_path = alt

        self._cache = {}

    @property
    def name(self) -> str:
        return "ai_composite"

    @property
    def min_lookback(self) -> int:
        return 1

    def evaluate(self, idx: int, days: list) -> Tuple[Optional[str], float]:
        """Standard evaluate interface — queries AI and classical forecasts."""
        station = getattr(self, '_station', None)
        if station is None:
            return None, 0.0

        if idx < 0 or idx >= len(days):
            return None, 0.0

        target_date = days[idx].get('date') if isinstance(days[idx], dict) else None
        if target_date is None:
            return None, 0.0

        return self.evaluate_for_station(station, str(target_date))

    def evaluate_for_station(self, station: str, date: str,
                              market_type: str = 'HIGH',
                              conn: sqlite3.Connection = None) -> Tuple[Optional[str], float]:
        """
        Evaluate AI composite signal for a specific station and date.

        Combines AI model predictions (AIGFS, GraphCast, AIFS) with classical
        NWP forecasts (GFS, ECMWF, ICON, GEM). Uses divergence between the two
        families as a confidence modulator.

        Args:
            station: Station code (e.g. 'KATL')
            date: ISO date string 'YYYY-MM-DD'
            market_type: 'HIGH' for max temp, 'LOW' for min temp
            conn: Optional SQLite connection

        Returns:
            (direction, confidence) or (None, 0.0) if insufficient data
        """
        try:
            today_dt = datetime.strptime(date, '%Y-%m-%d')
        except ValueError:
            return None, 0.0

        tomorrow_date = (today_dt + timedelta(days=1)).strftime('%Y-%m-%d')

        # Determine which temperature field to use
        classical_field = CLASSICAL_TEMP_FIELD_MAX if market_type == 'HIGH' else CLASSICAL_TEMP_FIELD_MIN

        # Collect AI model votes
        ai_votes = []
        ai_magnitudes = []

        for model in AI_MODELS:
            today_temp = self._get_ai_temp(station, date, model)
            tomorrow_temp = self._get_ai_temp(station, tomorrow_date, model)

            if today_temp is not None and tomorrow_temp is not None:
                mag = abs(tomorrow_temp - today_temp)
                ai_magnitudes.append(mag)
                if tomorrow_temp > today_temp:
                    ai_votes.append('up')
                elif tomorrow_temp < today_temp:
                    ai_votes.append('down')

        # Collect classical NWP model votes
        classical_votes = []
        classical_magnitudes = []

        for model in CLASSICAL_MODELS:
            today_temp = self._get_classical_temp(station, date, model, classical_field)
            tomorrow_temp = self._get_classical_temp(station, tomorrow_date, model, classical_field)

            if today_temp is not None and tomorrow_temp is not None:
                mag = abs(tomorrow_temp - today_temp)
                classical_magnitudes.append(mag)
                if tomorrow_temp > today_temp:
                    classical_votes.append('up')
                elif tomorrow_temp < today_temp:
                    classical_votes.append('down')

        # Need at least one vote from each family to compute divergence
        if not ai_votes and not classical_votes:
            return None, 0.0

        # If only one family has data, fall back to that family's consensus
        if not ai_votes:
            return self._family_consensus(classical_votes, classical_magnitudes, 'classical')
        if not classical_votes:
            return self._family_consensus(ai_votes, ai_magnitudes, 'ai')

        # Both families have data — compute divergence
        ai_direction = self._majority_direction(ai_votes)
        classical_direction = self._majority_direction(classical_votes)

        if ai_direction is None and classical_direction is None:
            return None, 0.0

        # If one family is tied, use the other
        if ai_direction is None:
            return self._family_consensus(classical_votes, classical_magnitudes, 'classical')
        if classical_direction is None:
            return self._family_consensus(ai_votes, ai_magnitudes, 'ai')

        # Compute divergence: do AI and classical agree?
        families_agree = (ai_direction == classical_direction)

        # Compute divergence magnitude: how different are the AI predictions
        # from classical predictions in terms of temperature change?
        divergence_magnitude = self._compute_divergence_magnitude(
            station, date, tomorrow_date, classical_field
        )

        # Use the more confident family's direction when they disagree
        ai_confidence = self._family_confidence(ai_votes, ai_magnitudes, len(ai_votes))
        classical_confidence = self._family_confidence(classical_votes, classical_magnitudes, len(classical_votes))

        if families_agree:
            direction = ai_direction  # Both agree
            # Boost confidence for agreement
            base_conf = max(ai_confidence, classical_confidence)
            confidence = min(0.98, base_conf + 0.08)
        else:
            # Use the more confident family's direction
            if ai_confidence >= classical_confidence:
                direction = ai_direction
            else:
                direction = classical_direction

            # Reduce confidence based on divergence
            base_conf = max(ai_confidence, classical_confidence)
            # Divergence penalty: 0.05-0.25 depending on disagreement magnitude
            divergence_penalty = min(0.25, 0.05 + (divergence_magnitude * 0.02))
            confidence = max(0.50, base_conf - divergence_penalty)

        return direction, round(confidence, 3)

    # ── Private helpers ──────────────────────────────────────────────

    def _majority_direction(self, votes: List[str]) -> Optional[str]:
        """Return majority direction, or None if tied."""
        if not votes:
            return None
        up = sum(1 for v in votes if v == 'up')
        down = sum(1 for v in votes if v == 'down')
        if up == down:
            return None
        return 'up' if up > down else 'down'

    def _family_consensus(self, votes: List[str], magnitudes: List[float],
                           family: str) -> Tuple[Optional[str], float]:
        """Produce a signal from a single family of models."""
        direction = self._majority_direction(votes)
        if direction is None:
            return None, 0.0
        confidence = self._family_confidence(votes, magnitudes, len(votes))
        return direction, confidence

    def _family_confidence(self, votes: List[str], magnitudes: List[float],
                            total_votes: int) -> float:
        """Compute confidence for a family of model votes."""
        if not votes:
            return 0.0

        up = sum(1 for v in votes if v == 'up')
        down = sum(1 for v in votes if v == 'down')
        total = up + down

        if total == 0:
            return 0.0

        # Agreement ratio
        agreement = max(up, down) / total

        # Magnitude-based confidence
        avg_mag = sum(magnitudes) / len(magnitudes) if magnitudes else 0
        if avg_mag < 0.5:
            mag_conf = 0.65
        elif avg_mag < 1.5:
            mag_conf = 0.80
        elif avg_mag < 5.0:
            mag_conf = 0.90
        else:
            mag_conf = 0.95

        # Blend: agreement matters more with more models
        if total >= 3:
            confidence = 0.6 * agreement + 0.4 * mag_conf
        elif total >= 2:
            confidence = 0.5 * agreement + 0.5 * mag_conf
        else:
            confidence = 0.3 * agreement + 0.7 * mag_conf

        return max(0.55, min(0.98, confidence))

    def _compute_divergence_magnitude(self, station: str, today_date: str,
                                       tomorrow_date: str,
                                       classical_field: str) -> float:
        """Compute how much AI and classical predictions diverge.

        Returns a magnitude in degrees Fahrenheit representing the average
        absolute difference between AI and classical temperature predictions
        for tomorrow.
        """
        ai_temps = []
        for model in AI_MODELS:
            t = self._get_ai_temp(station, tomorrow_date, model)
            if t is not None:
                ai_temps.append(t)

        classical_temps = []
        for model in CLASSICAL_MODELS:
            t = self._get_classical_temp(station, tomorrow_date, model, classical_field)
            if t is not None:
                classical_temps.append(t)

        if not ai_temps or not classical_temps:
            return 0.0

        ai_mean = sum(ai_temps) / len(ai_temps)
        classical_mean = sum(classical_temps) / len(classical_temps)

        return abs(ai_mean - classical_mean)

    def _get_ai_temp(self, station: str, target_date: str,
                      model: str) -> Optional[float]:
        """Get temperature from ai_forecasts table for an AI model.

        Averages 6-hourly forecasts (0, 6, 12, 18 UTC) for the target date
        to produce a daily mean temperature.
        """
        key = ('ai', station, target_date, model)
        if key in self._cache:
            return self._cache[key]

        if not os.path.exists(str(self.nwp_db_path)):
            return None

        try:
            conn = sqlite3.connect(str(self.nwp_db_path), timeout=30)
            cur = conn.cursor()
            cur.execute("""
                SELECT AVG(temp_f) FROM ai_forecasts
                WHERE station = ? AND model = ? AND forecast_date = ?
                AND temp_f IS NOT NULL AND temp_f > -100 AND temp_f < 150
            """, (station, model, target_date))
            row = cur.fetchone()
            conn.close()
            if row and row[0] is not None:
                val = float(row[0])
                self._cache[key] = val
                return val
        except Exception as e:
            logger.debug(f"AI temp lookup error {station} {target_date} {model}: {e}")

        self._cache[key] = None
        return None

    def _get_classical_temp(self, station: str, target_date: str,
                             model: str, field: str) -> Optional[float]:
        """Get temperature from nwp_forecasts table for a classical NWP model."""
        key = ('classical', station, target_date, model, field)
        if key in self._cache:
            return self._cache[key]

        if not os.path.exists(str(self.nwp_db_path)):
            return None

        try:
            conn = sqlite3.connect(str(self.nwp_db_path), timeout=30)
            cur = conn.cursor()
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
            logger.debug(f"Classical NWP lookup error {station} {target_date} {model} {field}: {e}")

        self._cache[key] = None
        return None

    def get_calibration_key(self) -> str:
        return self.name


# ── Standalone test ──────────────────────────────────────────────────

STATIONS = ['KATL', 'KAUS', 'KBOS', 'KDCA', 'KDEN', 'KDFW', 'KHOU', 'KLAS',
            'KLAX', 'KMDW', 'KMIA', 'KMSP', 'KMSY', 'KNYC', 'KOKC', 'KPHL',
            'KPHX', 'KSAT', 'KSEA', 'KSFO']


def test_signal():
    """Quick test of the signal."""
    s = AiCompositeSignal()
    print(f"Signal: {s.name}")
    for station in ['KNYC', 'KATL', 'KDCA']:
        for date in ['2026-06-01', '2026-06-15', '2026-07-01']:
            d, c = s.evaluate_for_station(station, date, 'HIGH')
            print(f"  {station} {date} HIGH: {d} @ {c:.3f}")
            d, c = s.evaluate_for_station(station, date, 'LOW')
            print(f"  {station} {date} LOW:  {d} @ {c:.3f}")
    print("Done")


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    test_signal()