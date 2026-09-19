"""
B.2 — Hierarchical Bayesian Calibration

Builds a 4-tier shrinkage estimator for per-station × per-condition calibration.

Architecture:
  Tier 1  Global mean (all stations pooled)              — <5 pairs
  Tier 2  Climate group deviation (shrunk to global)     — 5-20 pairs
  Tier 3  Station deviation (shrunk to group)            — 20-100 pairs
  Tier 4  Full per-condition (station × season/regime)  — >100 pairs

Each level shrinks toward its parent. Shrinkage factor k = n / (n + threshold)
provides smooth blending rather than hard cutoffs.

Climate groups:
  NORTHEAST: KNYC, KBOS, KDCA, KPHL, KMDW
  SOUTHEAST: KATL, KHOU, KMIA, KMSY, KDFW
  CENTRAL:   KDEN, KOKC, KMSP
  SOUTHWEST: KPHX, KLAS
  PACIFIC:   KLAX, KSFO, KSEA
  TEXAS:     KAUS, KSAT

Re-authored 2026-09-19 (decompilation was corrupted; algorithm re-specified from
the module docstring + tier thresholds).

Deterministic math only. No AI/ML in loop.
"""

import math
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

CLIMATE_GROUPS: Dict[str, List[str]] = {
    'NORTHEAST': ['KNYC', 'KBOS', 'KDCA', 'KPHL', 'KMDW'],
    'SOUTHEAST': ['KATL', 'KHOU', 'KMIA', 'KMSY', 'KDFW'],
    'CENTRAL': ['KDEN', 'KOKC', 'KMSP'],
    'SOUTHWEST': ['KPHX', 'KLAS'],
    'PACIFIC': ['KLAX', 'KSFO', 'KSEA'],
    'TEXAS': ['KAUS', 'KSAT'],
}

STATION_TO_GROUP: Dict[str, str] = {}
for _group, _stations in CLIMATE_GROUPS.items():
    for _s in _stations:
        STATION_TO_GROUP[_s] = _group

SEASONS = ['winter', 'spring', 'summer', 'fall']


def get_season(date_str: str) -> str:
    """Map a YYYY-MM-DD date to a meteorological season."""
    month = int(date_str.split('-')[1])
    if month in (12, 1, 2):
        return 'winter'
    if month in (3, 4, 5):
        return 'spring'
    if month in (6, 7, 8):
        return 'summer'
    return 'fall'


def climate_group(station: str) -> str:
    """Get climate group for a station."""
    return STATION_TO_GROUP.get(station.upper().strip(), 'OTHER')


class HierarchicalBayesianCalibration:
    """
    4-tier hierarchical Bayesian shrinkage estimator for calibration.

    Each estimate is a dict:
      - bias: mean forecast error (forecast - actual) in °F
      - mae: mean absolute error
      - rmse: root mean squared error
      - n: number of pairs
      - source: 'global' | 'group_shrunk' | 'station_shrunk' | 'full_condition'
      - weight: shrinkage factor k = n / (n + threshold)
    """

    TIER_THRESHOLDS = {
        'global_to_group': 5,
        'group_to_station': 20,
        'station_to_condition': 100,
    }

    def __init__(self, calibration_data: Optional[Dict] = None):
        self.calibration_data = calibration_data or {}
        self._global = None
        self._group = {}
        self._station = {}
        self._condition = {}

    # -- stats helpers -----------------------------------------------------
    @staticmethod
    def _stats(errors: List[float]) -> Dict[str, float]:
        n = len(errors)
        if n == 0:
            return {'bias': 0.0, 'mae': 0.0, 'rmse': 0.0, 'n': 0}
        mean = sum(errors) / n
        mae = sum(abs(e) for e in errors) / n
        rmse = math.sqrt(sum(e * e for e in errors) / n)
        return {'bias': mean, 'mae': mae, 'rmse': rmse, 'n': n}

    @staticmethod
    def _shrinkage(n: int, threshold: int) -> float:
        """Shrinkage factor k = n / (n + threshold)."""
        return n / (n + threshold)

    @staticmethod
    def _blend(parent: Dict, child: Dict, weight: float) -> Dict:
        """Blend child toward parent by weight (weight=0 -> parent, 1 -> child)."""
        w = max(0.0, min(1.0, weight))
        out = {}
        for key in ('bias', 'mae', 'rmse'):
            out[key] = parent.get(key, 0.0) * (1 - w) + child.get(key, 0.0) * w
        out['n'] = child.get('n', 0)
        out['weight'] = w
        return out

    # -- fitting -----------------------------------------------------------
    def fit(self, pairs: List[Tuple[str, str, float]]) -> Dict[str, Any]:
        """
        Fit the 4-tier hierarchy.

        Args:
            pairs: list of (station, date, error) where error = forecast - actual.

        Returns the fitted hierarchy dict.
        """
        # Tier 1: global.
        self._global = self._stats([e for (_, _, e) in pairs])

        # Group errors by climate group.
        group_errors: Dict[str, List[float]] = defaultdict(list)
        for station, _, e in pairs:
            group_errors[climate_group(station)].append(e)

        # Tier 2: group (shrunk to global).
        for group, errs in group_errors.items():
            raw = self._stats(errs)
            w = self._shrinkage(raw['n'], self.TIER_THRESHOLDS['global_to_group'])
            self._group[group] = self._blend(self._global, raw, w)
            self._group[group]['source'] = 'group_shrunk'

        # Tier 3: station (shrunk to group).
        station_errors: Dict[str, List[float]] = defaultdict(list)
        for station, _, e in pairs:
            station_errors[station].append(e)

        for station, errs in station_errors.items():
            raw = self._stats(errs)
            group_est = self._group.get(climate_group(station), self._global)
            w = self._shrinkage(raw['n'], self.TIER_THRESHOLDS['group_to_station'])
            self._station[station] = self._blend(group_est, raw, w)
            self._station[station]['source'] = 'station_shrunk'

        # Tier 4: per-condition (station × season), shrunk to station.
        condition_errors: Dict[Tuple[str, str], List[float]] = defaultdict(list)
        for station, date, e in pairs:
            condition_errors[(station, get_season(date))].append(e)

        for (station, season), errs in condition_errors.items():
            raw = self._stats(errs)
            station_est = self._station.get(station, self._global)
            w = self._shrinkage(raw['n'], self.TIER_THRESHOLDS['station_to_condition'])
            key = f"{station}:{season}"
            self._condition[key] = self._blend(station_est, raw, w)
            self._condition[key]['source'] = 'full_condition'

        return {
            'global': self._global,
            'group': dict(self._group),
            'station': dict(self._station),
            'condition': dict(self._condition),
        }

    def predict(self, station: str, date_str: Optional[str] = None) -> Dict:
        """Return the best available calibration estimate for a station (and date)."""
        station = station.upper().strip()
        if date_str:
            key = f"{station}:{get_season(date_str)}"
            if key in self._condition:
                return self._condition[key]
        if station in self._station:
            return self._station[station]
        if climate_group(station) in self._group:
            return self._group[climate_group(station)]
        return self._global or {'bias': 0.0, 'mae': 0.0, 'rmse': 0.0, 'n': 0}


if __name__ == '__main__':
    print('Hierarchical Bayesian Calibration — self-test')
    # Synthetic pairs: 3 stations, some error structure.
    pairs = []
    import random
    random.seed(0)
    for station in ['KNYC', 'KDEN', 'KPHX']:
        for d in range(1, 30):
            date = f"2026-{d % 12 + 1:02d}-{d % 28 + 1:02d}"
            err = random.gauss(1.0, 2.0)
            pairs.append((station, date, err))

    cal = HierarchicalBayesianCalibration()
    result = cal.fit(pairs)
    print('global bias:', round(result['global']['bias'], 3))
    print('groups:', sorted(result['group'].keys()))
    print('stations:', sorted(result['station'].keys()))
    print('KNYC predict:', {k: round(v, 3) if isinstance(v, float) else v
                            for k, v in cal.predict('KNYC').items()})
    print('Done.')
