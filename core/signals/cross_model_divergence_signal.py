#!/usr/bin/env python3
"""
ADVANCE Signal: Cross-Model Divergence (Differential Bias)

Computes divergence between bias-corrected GFS and ECMWF forecasts.
Models that agree after bias correction = high confidence.
Models that disagree after bias correction = low confidence.

Mechanism:
  1. Fetch GFS and ECMWF temperature_2m_max forecasts from nwp_forecasts.db
  2. Compute rolling bias for each model per station (14-day window)
  3. Apply bias correction: corrected = forecast + bias_adjustment
  4. Compute divergence = |GFS_corrected - ECMWF_corrected|
  5. Confidence = 1 - normalized_divergence (clamped to [0, 1])

B-Mode compliant. No AI/ML. No API calls (uses existing NWP DB).

Usage:
    from core.signals.cross_model_divergence_signal import CrossModelDivergenceSignal

    signal = CrossModelDivergenceSignal()
    result = signal.get_divergence(station="KNYC", target_date="2026-08-01")
    # Returns: { 'divergence_f': 2.1, 'confidence': 0.79, 'direction': 'up', ... }
"""

import logging
import math
import sqlite3
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

from .base_signal import BaseSignal

# Default DB paths
DEFAULT_NWP_DB_PATH = "/home/node/.openclaw/workspace/prototypes/weather-engine-source/data/nwp_forecasts.db"
DEFAULT_METAR_DB_PATH = "/home/node/.openclaw/workspace/prototypes/weather-engine-source/data/metar_backfill.db"

# Rolling bias window
BIAS_WINDOW_DAYS = 14
MIN_BIAS_OBSERVATIONS = 7
MAX_BIAS_MAGNITUDE_F = 5.0

# Divergence normalization
# Below this threshold (in °F), divergence is considered negligible and confidence is maximal
MIN_DIVERGENCE_F = 0.5
# Above this threshold, divergence is considered extreme and confidence is minimal
MAX_DIVERGENCE_F = 8.0


class CrossModelDivergenceSignal(BaseSignal):
    """
    Cross-model divergence signal using bias-corrected GFS and ECMWF forecasts.

    Measures how much two top-performing NWP models agree after removing
    each model's systematic bias. Agreement = high confidence, disagreement
    = low confidence. This is a meta-signal that modulates other signals
    rather than producing independent direction.
    """

    def __init__(self, db_path: Optional[str] = None,
                 nwp_db_path: Optional[str] = None,
                 metar_db_path: Optional[str] = None):
        super().__init__(db_path)
        self.nwp_db_path = nwp_db_path or DEFAULT_NWP_DB_PATH
        self.metar_db_path = metar_db_path or DEFAULT_METAR_DB_PATH

    # ─── Forecast Fetching ───────────────────────────────────────────────────

    def _fetch_forecast(self, station: str, target_date: str,
                        model: str) -> Optional[float]:
        """
        Fetch temperature_2m_max forecast for a model from nwp_forecasts.db.

        Args:
            station: ICAO code (e.g., 'KNYC')
            target_date: YYYY-MM-DD target date
            model: 'gfs' or 'ecmwf'

        Returns: forecast temperature in °F, or None
        """
        try:
            conn = sqlite3.connect(self.nwp_db_path)
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA busy_timeout=5000;")
            cur = conn.cursor()

            # Get the most recent forecast for this model/station/date
            cur.execute("""
                SELECT value
                FROM nwp_forecasts
                WHERE station = ? AND target_date = ?
                  AND model = ? AND variable = 'temperature_2m_max'
                ORDER BY fetch_timestamp DESC
                LIMIT 1
            """, (station, target_date, model))

            row = cur.fetchone()
            conn.close()
            return float(row[0]) if row else None
        except Exception as e:
            logger.warning(f"Failed to fetch {model} forecast for {station} "
                           f"on {target_date}: {e}")
            return None

    def _fetch_forecasts_both_models(self, station: str,
                                     target_date: str) -> Tuple[Optional[float], Optional[float]]:
        """
        Fetch both GFS and ECMWF forecasts for a station/date.

        Returns: (gfs_temp, ecmwf_temp)
        """
        gfs = self._fetch_forecast(station, target_date, 'gfs')
        ecmwf = self._fetch_forecast(station, target_date, 'ecmwf')
        return gfs, ecmwf

    # ─── Bias Computation ───────────────────────────────────────────────────

    def _compute_model_bias(self, station: str, model: str) -> Optional[float]:
        """
        Compute rolling bias for a specific model at a station.

        Bias = mean(actual - forecast) over the last BIAS_WINDOW_DAYS days.
        Positive bias = model under-predicts (actual higher than forecast).
        Negative bias = model over-predicts (actual lower than forecast).

        Uses daily_stats for actual max temps and nwp_forecasts for forecast temps.

        Args:
            station: ICAO code
            model: 'gfs' or 'ecmwf'

        Returns: mean bias in °F, or None if insufficient data
        """
        try:
            conn = sqlite3.connect(self.metar_db_path)
            conn.execute("PRAGMA busy_timeout=5000;")

            # Get daily_stats actual highs for the last BIAS_WINDOW_DAYS
            cutoff = (datetime.now() - timedelta(days=BIAS_WINDOW_DAYS)).strftime('%Y-%m-%d')
            today = datetime.now().strftime('%Y-%m-%d')

            cur = conn.cursor()
            cur.execute("""
                SELECT date_utc, MAX(temp_f) as actual_high
                FROM metar_observations
                WHERE station = ? AND date_utc >= ? AND date_utc < ?
                  AND temp_f IS NOT NULL
                GROUP BY date_utc
                ORDER BY date_utc
            """, (station, cutoff, today))

            actual_days = {r[0]: float(r[1]) for r in cur.fetchall()}
            conn.close()

            if not actual_days:
                return None

            # Now get model forecasts for these same dates from nwp_forecasts.db
            nwp_conn = sqlite3.connect(self.nwp_db_path)
            nwp_conn.execute("PRAGMA journal_mode=WAL;")
            nwp_conn.execute("PRAGMA busy_timeout=5000;")
            nwp_cur = nwp_conn.cursor()

            biases = []
            for date_str, actual_high in actual_days.items():
                nwp_cur.execute("""
                    SELECT value FROM nwp_forecasts
                    WHERE station = ? AND target_date = ?
                      AND model = ? AND variable = 'temperature_2m_max'
                    ORDER BY fetch_timestamp DESC
                    LIMIT 1
                """, (station, date_str, model))

                row = nwp_cur.fetchone()
                if row and row[0] is not None:
                    # nwp_forecasts.db stores temperature in Celsius; METAR actuals are in Fahrenheit.
                    # Convert forecast to Fahrenheit before computing bias.
                    forecast_c = float(row[0])
                    forecast_f = forecast_c * 9.0 / 5.0 + 32.0
                    bias = actual_high - forecast_f
                    biases.append(bias)

            nwp_conn.close()

            if len(biases) < MIN_BIAS_OBSERVATIONS:
                return None

            # Cap individual biases before averaging
            capped_biases = [max(-MAX_BIAS_MAGNITUDE_F,
                                 min(MAX_BIAS_MAGNITUDE_F, b))
                             for b in biases]

            mean_bias = sum(capped_biases) / len(capped_biases)
            return round(mean_bias, 2)

        except Exception as e:
            logger.warning(f"Failed to compute {model} bias for {station}: {e}")
            return None

    # ─── Divergence Computation ─────────────────────────────────────────────

    def get_divergence(self, station: str, target_date: str,
                       gfs_temp: Optional[float] = None,
                       ecmwf_temp: Optional[float] = None,
                       prev_day_high: Optional[float] = None) -> Dict:
        """
        Compute bias-corrected divergence between GFS and ECMWF.

        Args:
            station: ICAO code
            target_date: YYYY-MM-DD
            gfs_temp: optional pre-fetched GFS forecast (auto-fetches if None)
            ecmwf_temp: optional pre-fetched ECMWF forecast (auto-fetches if None)
            prev_day_high: optional previous day's actual high (for direction)

        Returns: dict with keys:
            - divergence_f: raw divergence in °F
            - confidence: float [0, 1] (1 = models agree, 0 = disagree)
            - gfs_corrected: bias-corrected GFS temp (°F)
            - ecmwf_corrected: bias-corrected ECMWF temp (°F)
            - gfs_bias: GFS bias applied (°F)
            - ecmwf_bias: ECMWF bias applied (°F)
            - gfs_raw: raw GFS forecast (°C, as stored in nwp_forecasts.db)
            - ecmwf_raw: raw ECMWF forecast (°C, as stored in nwp_forecasts.db)
            - gfs_raw_f: raw GFS forecast (°F)
            - ecmwf_raw_f: raw ECMWF forecast (°F)
            - direction: 'up' or 'down' (if prev_day_high provided)
            - n_sources: 0, 1, or 2 (how many models had data)
        """
        # Auto-fetch if not provided
        if gfs_temp is None or ecmwf_temp is None:
            fetched_gfs, fetched_ecmwf = self._fetch_forecasts_both_models(
                station, target_date)
            if gfs_temp is None:
                gfs_temp = fetched_gfs
            if ecmwf_temp is None:
                ecmwf_temp = fetched_ecmwf

        n_sources = sum(1 for t in [gfs_temp, ecmwf_temp] if t is not None)

        # Compute bias for each model (bias is in Fahrenheit)
        gfs_bias = self._compute_model_bias(station, 'gfs')
        ecmwf_bias = self._compute_model_bias(station, 'ecmwf')

        # nwp_forecasts.db stores temperatures in Celsius. Convert to Fahrenheit
        # so bias correction and divergence are computed in consistent units.
        def _to_fahrenheit(celsius):
            if celsius is None:
                return None
            return celsius * 9.0 / 5.0 + 32.0

        gfs_deg_f = _to_fahrenheit(gfs_temp)
        ecmwf_deg_f = _to_fahrenheit(ecmwf_temp)

        # Apply bias correction (bias in Fahrenheit)
        gfs_corrected = None
        ecmwf_corrected = None

        if gfs_deg_f is not None:
            gfs_corrected = gfs_deg_f + (gfs_bias if gfs_bias is not None else 0.0)

        if ecmwf_deg_f is not None:
            ecmwf_corrected = ecmwf_deg_f + (ecmwf_bias if ecmwf_bias is not None else 0.0)

        # Compute divergence
        divergence_f = None
        confidence = 0.5  # Default: neutral (no signal, no anti-signal)

        if gfs_corrected is not None and ecmwf_corrected is not None:
            divergence_f = abs(gfs_corrected - ecmwf_corrected)

            # Normalize: confidence = 1 - normalized_divergence
            # Map divergence_f from [MIN_DIVERGENCE_F, MAX_DIVERGENCE_F] to [1.0, 0.0]
            if divergence_f <= MIN_DIVERGENCE_F:
                confidence = 1.0
            elif divergence_f >= MAX_DIVERGENCE_F:
                confidence = 0.0
            else:
                confidence = 1.0 - (divergence_f - MIN_DIVERGENCE_F) / \
                                  (MAX_DIVERGENCE_F - MIN_DIVERGENCE_F)

            confidence = round(confidence, 3)
            divergence_f = round(divergence_f, 2)

        # Determine direction from bias-corrected consensus
        direction = None
        if prev_day_high is not None:
            consensus = None
            if gfs_corrected is not None and ecmwf_corrected is not None:
                consensus = (gfs_corrected + ecmwf_corrected) / 2.0
            elif gfs_corrected is not None:
                consensus = gfs_corrected
            elif ecmwf_corrected is not None:
                consensus = ecmwf_corrected

            if consensus is not None:
                delta = consensus - prev_day_high
                if abs(delta) >= 2.0:  # MIN_CONSENSUS_DELTA threshold
                    direction = 'up' if delta > 0 else 'down'

        return {
            'divergence_f': divergence_f,
            'confidence': confidence,
            'gfs_corrected': round(gfs_corrected, 1) if gfs_corrected is not None else None,
            'ecmwf_corrected': round(ecmwf_corrected, 1) if ecmwf_corrected is not None else None,
            'gfs_bias': gfs_bias,
            'ecmwf_bias': ecmwf_bias,
            'gfs_raw': gfs_temp,
            'ecmwf_raw': ecmwf_temp,
            'gfs_raw_f': round(gfs_deg_f, 1) if gfs_deg_f is not None else None,
            'ecmwf_raw_f': round(ecmwf_deg_f, 1) if ecmwf_deg_f is not None else None,
            'direction': direction,
            'n_sources': n_sources,
        }

    # ─── Convenience: Confidence Modulator ──────────────────────────────────

    def get_confidence_modulator(self, station: str, target_date: str,
                                 base_confidence: float) -> float:
        """
        Get a confidence adjustment factor based on cross-model divergence.

        Combines with another signal's confidence: adjusted_confidence =
        base_confidence * sqrt(modulator).

        When models agree (high confidence from divergence), the modulator
        is near 1.0 (no change). When models disagree, the modulator is
        low, suppressing the base confidence.

        Args:
            station: ICAO code
            target_date: YYYY-MM-DD
            base_confidence: the other signal's confidence [0, 1]

        Returns: modulator in [0, 1] to multiply with base_confidence
        """
        result = self.get_divergence(station, target_date)
        divergence_conf = result['confidence']

        # If we couldn't compute divergence, return neutral modulator
        if result['n_sources'] < 2:
            return 1.0

        # Use sqrt so the effect is gentler
        return math.sqrt(divergence_conf)

    # ─── BaseSignal Interface ──────────────────────────────────────────────

    @property
    def name(self) -> str:
        """Canonical name for this signal."""
        return "cross_model_divergence"

    @property
    def min_lookback(self) -> int:
        """Minimum number of prior days required for this signal to fire."""
        return 14

    def evaluate(self, idx: int, days: List[dict]) -> Tuple[Optional[str], float]:
        """
        Evaluate the signal at day index `idx` given historical `days`.

        This meta-signal requires NWP database access and station context
        to compute cross-model divergence. The standard evaluate() path
        cannot fire independently; use evaluate_for_station() instead.

        Returns: (direction, confidence) as (None, 0.5) — neutral.
        """
        return None, 0.5

    def evaluate_for_station(self, station: str, date: str,
                             conn: sqlite3.Connection = None) -> Tuple[Optional[str], float]:
        """
        Evaluate cross-model divergence for a specific station and date.

        Fetches bias-corrected divergence from NWP forecasts and returns
        directional prediction with modulated confidence.

        Args:
            station: Station code (e.g. 'KNYC')
            date: ISO date string 'YYYY-MM-DD'
            conn: Optional SQLite connection to metar DB

        Returns:
            (direction, confidence) — direction is 'up'/'down' or None,
            confidence is the divergence-based modulator.
        """
        # Get previous day's actual high for direction
        prev_day_high = None
        own_conn = conn is None
        if own_conn:
            try:
                conn = sqlite3.connect(self.metar_db_path)
            except Exception:
                pass

        try:
            if conn is not None:
                cur = conn.cursor()
                prev_date = (datetime.strptime(date, '%Y-%m-%d') -
                             timedelta(days=1)).strftime('%Y-%m-%d')
                cur.execute("""
                    SELECT MAX(temp_f) FROM metar_observations
                    WHERE station=? AND date_utc=? AND temp_f IS NOT NULL
                """, (station, prev_date))
                row = cur.fetchone()
                if row and row[0] is not None:
                    prev_day_high = float(row[0])
        except Exception:
            logger.warning(f"Failed to fetch prev day high for {station} on {date}")
        finally:
            if own_conn and conn:
                conn.close()

        result = self.get_divergence(station, date, prev_day_high=prev_day_high)
        direction = result.get('direction')
        confidence = self.get_confidence_modulator(station, date, 1.0)
        return direction, confidence


# ─── Standalone test ────────────────────────────────────────────────────────

def main():
    """Run a quick test of the cross-model divergence signal."""
    import sys
    sys.path.insert(0, '/home/node/.openclaw/workspace/prototypes/weather-engine-source')

    signal = CrossModelDivergenceSignal()

    test_cases = [
        ("KNYC", "2026-08-01"),
        ("KATL", "2026-08-01"),
        ("KLAX", "2026-08-01"),
        ("KLAS", "2026-08-01"),
        ("KDFW", "2026-08-01"),
        ("KNYC", "2026-07-15"),
        ("KNYC", "2026-06-01"),
    ]

    print(f"{'='*95}")
    print("CROSS-MODEL DIVERGENCE SIGNAL TEST")
    print(f"{'='*95}")
    print(f"{'Station':<8} {'Date':<12} {'GFS°C':>6} {'ECM°C':>6} "
          f"{'GFS°F':>7} {'ECM°F':>7} {'GFSb':>5} "
          f"{'ECMWFb':>6} {'GFS_c°F':>8} {'ECM_c°F':>8} "
          f"{'Div°F':>6} {'Conf':>5} {'Dir':>4}")
    print("-" * 95)

    for station, target_date in test_cases:
        result = signal.get_divergence(station, target_date, prev_day_high=85.0)
        gfs_r_c = result['gfs_raw']
        ecmwf_r_c = result['ecmwf_raw']
        gfs_r_f = result['gfs_raw_f']
        ecmwf_r_f = result['ecmwf_raw_f']
        gfs_b = result['gfs_bias']
        ecmwf_b = result['ecmwf_bias']
        gfs_c = result['gfs_corrected']
        ecmwf_c = result['ecmwf_corrected']
        div = result['divergence_f']
        conf = result['confidence']
        direction = result['direction'] or '--'

        print(f"{station:<8} {target_date:<12} "
              f"{gfs_r_c or '--':>6} {ecmwf_r_c or '--':>6} "
              f"{gfs_r_f or '--':>7} {ecmwf_r_f or '--':>7} "
              f"{gfs_b or '--':>5} {ecmwf_b or '--':>6} "
              f"{gfs_c or '--':>8} {ecmwf_c or '--':>8} "
              f"{div or '--':>6} {conf:>5.2f} {direction:>4}")

    print(f"\n{'='*80}")
    print("Test complete.")
    print(f"{'='*80}")


if __name__ == "__main__":
    main()