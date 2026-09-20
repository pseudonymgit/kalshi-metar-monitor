#!/usr/bin/env python3
# NWP DIRECT SIGNAL — STRIKE-CROSSING AXIS (R/R-1b re-spec, 2026-09-20)
#
# HISTORY / DEFECT:
#   This signal previously predicted DAY-OVER-DAY direction
#   (`tomorrow_temp > today_temp` -> 'up').  That answers "will tomorrow be
#   warmer than today", but it was SCORED against the STRIKE-CROSSING axis
#   (`settlement_temp_f` vs `strike_price`).  That is a target-variable
#   mismatch — the same defect class `core/settlement_outcome.py` was built to
#   eliminate.  It produced a spurious +0.114 MCC on `nwp_dtdt_fusion` in the
#   R/R-1a Part-1 re-score (an AXIS ARTIFACT, not skill).
#
# RE-SPEC (R/R-1b):
#   The signal now emits a directional vote on whether the NWP forecast of the
#   settlement day's EXTREMUM crosses the strike price:
#     HIGH market -> forecast of temperature_2m_max  (°C -> °F) vs strike_price
#                    'up'   if forecast_max > strike   (contract won)
#                    'down' if forecast_max <= strike  (contract lost; incl tie)
#     LOW  market -> forecast of temperature_2m_min  (°C -> °F) vs strike_price
#                    'down' if forecast_min < strike   (contract won)
#                    'up'   if forecast_min >= strike  (contract lost; incl tie)
#   Tie/equal is a LOSS (strict > / <), matching `core/settlement_outcome.py`.
#
#   Strike price is a REQUIRED input.  Without it the signal cannot answer the
#   strike-crossing question and MUST return (None, 0.0) — it must NOT fall back
#   to day-over-day comparison (that fallback is the bug this re-spec removes).
#
# DATA / UNITS:
#   NWP DB stores temperature_2m_max / temperature_2m_min in °C.  The strike
#   axis (`settlement_temp_f`, `strike_price`) is °F.  Convert forecast °C -> °F.
#   The NWP DB DOES contain the target-day extrema (no data gap) — re-spec is
#   feasible, not a kill.
#
# LOOKAHEAD:
#   Contemporaneous-only gate (`fetch_date <= target_date`) via
#   `core/nwp_lookahead_gate.contemporaneous_where` — already wired.
#
# CHANGELOG:
#   R/R-1b (2026-09-20): re-spec'd day-over-day -> strike-crossing axis.
#

import sqlite3
import os
from pathlib import Path
from datetime import datetime
from typing import Optional, Tuple, Dict
import logging
from .base_signal import BaseSignal

logger = logging.getLogger(__name__)

NWP_DB_DEFAULT = "data/nwp_forecasts.db"

# Models in order of preference (all provide temperature_2m_max / _min).
MODELS = ['GFS', 'ECMWF', 'ICON', 'GEM']


def _c_to_f(c: float) -> float:
    return c * 9.0 / 5.0 + 32.0


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
        """Standard evaluate interface — needs a strike to be meaningful.

        A strike-crossing vote is undefined without a strike_price.  If the
        caller has set `self._strike_price`, use it; otherwise return no signal
        (do NOT silently fall back to day-over-day — that fallback is the bug).
        """
        station = getattr(self, '_station', None)
        if station is None:
            return None, 0.0
        if idx < 0 or idx >= len(days):
            return None, 0.0
        target_date = days[idx].get('date') if isinstance(days[idx], dict) else None
        if target_date is None:
            return None, 0.0
        market_type = getattr(self, '_market_type', 'HIGH')
        strike_price = getattr(self, '_strike_price', None)
        return self.evaluate_for_station(
            station, str(target_date), market_type=market_type,
            strike_price=strike_price)

    def evaluate_for_station(self, station: str, date: str,
                              market_type: str = 'HIGH',
                              strike_price: Optional[float] = None,
                              conn: sqlite3.Connection = None) -> Tuple[Optional[str], float]:
        """Evaluate the strike-crossing direction for (station, date, market_type).

        Args:
            station:     station code (e.g. 'KATL').
            date:        settlement (target) date 'YYYY-MM-DD'.
            market_type: 'HIGH' (predict high > strike) or 'LOW' (predict low < strike).
            strike_price: the strike to cross (REQUIRED; °F).  None -> no signal.
            conn:        unused (kept for interface symmetry).

        Returns:
            (direction, confidence) or (None, 0.0) if strike missing / no data.
        """
        if strike_price is None:
            # Strike-crossing is undefined without a strike.  Do NOT fall back
            # to day-over-day.
            return None, 0.0

        mt = (market_type or 'HIGH').upper()
        if mt == 'HIGH':
            field = 'temperature_2m_max'
        elif mt == 'LOW':
            field = 'temperature_2m_min'
        else:
            # Only HIGH/LOW have single-strike crossing semantics.
            return None, 0.0

        try:
            strike_f = float(strike_price)
        except (TypeError, ValueError):
            return None, 0.0

        # Collect per-model votes on whether the forecast extremum crosses the
        # strike, on the correct axis.
        up = 0
        down = 0
        margins_f = []  # |forecast - strike| in °F, for confidence

        for model in self._model_order:
            ext_c = self._get_temp(station, date, model, field)
            if ext_c is None:
                continue
            ext_f = _c_to_f(ext_c)
            margin_f = ext_f - strike_f
            margins_f.append(abs(margin_f))
            if mt == 'HIGH':
                # win if high > strike
                if ext_f > strike_f:
                    up += 1
                else:
                    down += 1
            else:  # LOW
                # win if low < strike
                if ext_f < strike_f:
                    down += 1
                else:
                    up += 1

        total = up + down
        if total == 0:
            return None, 0.0

        if up == down:
            # Ambiguous: models are split evenly.  Return the majority of the
            # higher-confidence side?  No — return no signal (unresolved).
            return None, 0.0

        direction = 'up' if up > down else 'down'

        # Confidence: margin-based (how far the forecast sits from the strike),
        # plus model-agreement boost.  Deterministic.
        avg_margin_f = sum(margins_f) / len(margins_f) if margins_f else 0.0
        conf = self._margin_confidence(avg_margin_f)

        agreement = max(up, down) / total
        if agreement >= 0.8 and total >= 3:
            conf = min(0.98, conf + 0.03)
        elif agreement < 0.6 and total >= 2:
            conf = max(0.55, conf - 0.10)

        return direction, conf

    def _margin_confidence(self, margin_f: float) -> float:
        """Confidence from the forecast-vs-strike margin in °F."""
        if margin_f < 0.5:
            return 0.70
        elif margin_f < 1.5:
            return 0.85
        elif margin_f < 5.0:
            return 0.95
        else:
            return 0.98

    def _get_temp(self, station: str, target_date: str, model: str,
                  field: str = 'temperature_2m_max') -> Optional[float]:
        """Get the target-day extremum forecast (°C) from the NWP DB."""
        key = (station, target_date, model, field)
        if key in self._cache:
            return self._cache[key]

        if not os.path.exists(str(self.nwp_db_path)):
            return None

        try:
            conn = sqlite3.connect(str(self.nwp_db_path), timeout=30)
            cur = conn.cursor()
            # Contemporaneous-only gate (P0-F): exclude lookahead rows where
            # fetch_date > target_date (reanalysis/backfill masquerading as a
            # forecast).
            from ..nwp_lookahead_gate import contemporaneous_where
            cur.execute("""
                SELECT AVG(value) FROM nwp_forecasts
                WHERE station = ? AND target_date = ? AND LOWER(model) = ? AND variable = ?
                AND value IS NOT NULL AND value > -100 AND value < 150
            """ + contemporaneous_where(""), (station, target_date, model.lower(), field))
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

    def compute_signal(self, station: str, target_date: str = None,
                       market_type: str = 'HIGH',
                       strike_price: Optional[float] = None) -> Optional[Dict]:
        """Public dict-returning API (compatibility with NwpAnalogSignal callers)."""
        if target_date is None:
            target_date = datetime.now().strftime('%Y-%m-%d')
        direction, confidence = self.evaluate_for_station(
            station, target_date, market_type=market_type, strike_price=strike_price)
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
    """Quick test of the signal (requires a strike)."""
    s = NwpDirectSignal()
    print(f"Signal: {s.name}")
    for station in ['KNYC', 'KATL', 'KDCA']:
        for date in ['2026-06-01', '2026-06-15', '2026-07-01']:
            for mt, strike in [('HIGH', 85.0), ('LOW', 65.0)]:
                d, c = s.evaluate_for_station(station, date, market_type=mt, strike_price=strike)
                print(f"  {station} {date} {mt}@{strike}: {d} @ {c:.3f}" if d else f"  {station} {date} {mt}@{strike}: no signal")
    print("Done")


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    test_signal()
