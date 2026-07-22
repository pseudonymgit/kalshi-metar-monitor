#!/usr/bin/env python3

# CHANGELOG (last 10 broad changes):
# 1. [2026-07-13 A1: Fix alert dummy data - add bucket fallback to most recent available data]
# 2. [2026-07-05 R4-1.1: Fix P&L mark-to-market + thread-safe price cache]
#

"""
P2: 3-of-4 Ensemble Agreement Gate

Reads real NWP forecasts from data/nwp_forecasts.db (GFS, ECMWF, ICON, GEM).
For each station + target_date, determines each model's direction (UP/DOWN)
vs the previous day's actual high temperature. Only emits a signal when
≥3 of 4 models agree on direction. When <3 agree → FLAT (no signal).

This is a noise filter, not a standalone signal. It gates the multi-model
consensus to only fire on high-agreement days.

Usage:
    from core.ensemble_agreement import EnsembleAgreementGate
    gate = EnsembleAgreementGate()
    signal = gate.evaluate(station='KNYC', target_date='2026-07-04')
    # Returns: {'direction': 'up'|'down'|None, 'agreement': 3|4|None,
    #           'models': {...}, 'consensus_temp': float|None}
"""

import sqlite3
import math
import os
import argparse
from pathlib import Path
from datetime import datetime, timezone, timedelta
from collections import defaultdict

# ─── Paths ──────────────────────────────────────────────────────────────────
# Configuration: Base paths can be set via environment variable
BASE_PATH = Path(__file__).resolve().parents[1]  # Go up to repo root
NWP_DB_DEFAULT = "data/nwp_forecasts.db"
METAR_DB_DEFAULT = "data/metar_backfill.db"

# Get paths from environment variables, fall back to defaults
NWP_DB = os.environ.get('NWP_DB_PATH', BASE_PATH / NWP_DB_DEFAULT)
METAR_DB = os.environ.get('METAR_DB_PATH', BASE_PATH / METAR_DB_DEFAULT)

# Convert to Path objects and ensure directories exist
NWP_DB = Path(NWP_DB).resolve()
METAR_DB = Path(METAR_DB).resolve()

NWP_DB.parent.mkdir(parents=True, exist_ok=True)
METAR_DB.parent.mkdir(parents=True, exist_ok=True)

# Ensure the databases exist by connecting once
if not NWP_DB.exists():
    # Initialize with empty connection to create the file at least
    init_db = sqlite3.connect(NWP_DB)
    init_db.close()
if not METAR_DB.exists():
    # Initialize with empty connection to create the file at least
    init_db = sqlite3.connect(METAR_DB)
    init_db.close()

# ─── Config ─────────────────────────────────────────────────────────────────
MODELS = ['gfs', 'ecmwf', 'icon', 'gem']
MIN_AGREEMENT = 3          # 3-of-4 required to signal
MIN_DELTA_F = 0.5          # Min |forecast - prev_actual| to count as directional
HIGH_TEMP_VAR = 'temperature_2m_max'  # Daily max temp variable in NWP DB

# 20 Kalshi stations with coordinates (from nwp_collect.py)
STATIONS = {
    'KATL': ('Atlanta', 33.64, -84.43),
    'KAUS': ('Austin', 30.20, -97.67),
    'KBOS': ('Boston', 42.37, -71.01),
    'KDCA': ('Washington DC', 38.85, -77.04),
    'KDEN': ('Denver', 39.86, -104.67),
    'KDFW': ('Dallas', 32.90, -97.04),
    'KHOU': ('Houston', 29.98, -95.36),
    'KLAS': ('Las Vegas', 36.08, -115.16),
    'KLAX': ('Los Angeles', 33.94, -118.41),
    'KMDW': ('Chicago', 41.79, -87.75),
    'KMIA': ('Miami', 25.80, -80.29),
    'KMSP': ('Minneapolis', 44.88, -93.22),
    'KMSY': ('New Orleans', 29.99, -90.26),
    'KNYC': ('New York', 40.71, -74.01),
    'KOKC': ('Oklahoma City', 35.39, -97.60),
    'KPHL': ('Philadelphia', 39.87, -75.24),
    'KPHX': ('Phoenix', 33.43, -112.01),
    'KSAT': ('San Antonio', 29.53, -98.47),
    'KSEA': ('Seattle', 47.45, -122.31),
    'KSFO': ('San Francisco', 37.62, -122.38),
}


class EnsembleAgreementGate:
    """3-of-4 ensemble agreement gate using real NWP forecast data."""

    def __init__(self, nwp_db=NWP_DB, metar_db=METAR_DB):
        self.nwp_db = nwp_db
        self.metar_db = metar_db

    def _get_nwp_forecasts(self, station, target_date):
        """Get all model forecasts for a station on a target date."""
        conn = sqlite3.connect(self.nwp_db, timeout=10)
        c = conn.cursor()
        c.execute("""
            SELECT model, value FROM nwp_forecasts
            WHERE station=? AND target_date=? AND variable=?
        """, (station, target_date, HIGH_TEMP_VAR))
        result = {row[0]: row[1] for row in c.fetchall()}
        conn.close()
        return result

    def _get_prev_day_actual(self, station, target_date):
        """Get previous day's actual high temp from daily_stats."""
        td = datetime.strptime(target_date, '%Y-%m-%d')
        prev = (td - timedelta(days=1)).strftime('%Y-%m-%d')
        conn = sqlite3.connect(self.metar_db, timeout=10)
        c = conn.cursor()
        c.execute("""
            SELECT max_temp_f FROM daily_stats
            WHERE station=? AND date_utc=?
        """, (station, prev))
        row = c.fetchone()
        conn.close()
        return row[0] if row else None

    def _model_direction(self, forecast_temp, prev_actual):
        """Determine a single model's direction (up/down/flat)."""
        if forecast_temp is None or prev_actual is None:
            return None
        delta = forecast_temp - prev_actual
        if abs(delta) < MIN_DELTA_F:
            return 'flat'
        return 'up' if delta > 0 else 'down'

    def evaluate(self, station, target_date):
        """
        Evaluate the 3-of-4 agreement gate for a station on a target date.

        Returns dict:
            direction: 'up'|'down'|None  (None = flat / insufficient agreement)
            agreement: int (how many models agree, or None)
            models: {model: {forecast, direction}} per model
            consensus_temp: weighted mean of agreeing models, or None
            prev_actual: previous day's actual high
        """
        forecasts = self._get_nwp_forecasts(station, target_date)
        prev_actual = self._get_prev_day_actual(station, target_date)

        if prev_actual is None:
            return self._empty_result(target_date, station, forecasts, None)

        # Compute per-model direction
        model_directions = {}
        for model in MODELS:
            ft = forecasts.get(model)
            if ft is not None:
                model_directions[model] = {
                    'forecast': ft,
                    'direction': self._model_direction(ft, prev_actual),
                    'delta': ft - prev_actual
                }
            else:
                model_directions[model] = {
                    'forecast': None,
                    'direction': None,
                    'delta': None
                }

        # Count directional votes (exclude flat/None)
        up_count = sum(1 for m in model_directions.values()
                       if m['direction'] == 'up')
        down_count = sum(1 for m in model_directions.values()
                         if m['direction'] == 'down')
        directional_total = up_count + down_count

        # 3-of-4 agreement gate
        if up_count >= MIN_AGREEMENT:
            direction = 'up'
            agreement = up_count
        elif down_count >= MIN_AGREEMENT:
            direction = 'down'
            agreement = down_count
        else:
            direction = None
            agreement = directional_total if directional_total > 0 else None

        # Consensus temp: mean of models agreeing with the gate direction
        consensus_temp = None
        if direction is not None:
            agreeing = [model_directions[m]['forecast']
                        for m in MODELS
                        if model_directions[m]['direction'] == direction]
            if agreeing:
                consensus_temp = sum(agreeing) / len(agreeing)

        return {
            'station': station,
            'target_date': target_date,
            'direction': direction,
            'agreement': agreement,
            'models': model_directions,
            'consensus_temp': consensus_temp,
            'prev_actual': prev_actual,
            'up_count': up_count,
            'down_count': down_count,
            'flat_count': 4 - directional_total,
        }

    def _empty_result(self, target_date, station, forecasts, prev_actual):
        return {
            'station': station,
            'target_date': target_date,
            'direction': None,
            'agreement': None,
            'models': {m: {'forecast': forecasts.get(m),
                           'direction': None, 'delta': None}
                       for m in MODELS},
            'consensus_temp': None,
            'prev_actual': prev_actual,
            'up_count': 0,
            'down_count': 0,
            'flat_count': 4,
        }

    def evaluate_all_stations(self, target_date):
        """Evaluate the gate for all 20 stations on a target date."""
        return {station: self.evaluate(station, target_date)
                for station in STATIONS}

    def backtest(self, start_date=None, end_date=None):
        """
        Run the agreement gate over historical NWP data and compare
        against settlement epochs (HIGH market: up/down vs prev day).

        Returns summary stats: total days, coverage, accuracy on consensus days.
        """
        conn_nwp = sqlite3.connect(self.nwp_db, timeout=10)
        conn_metar = sqlite3.connect(self.metar_db, timeout=10)
        cn = conn_nwp.cursor()
        cm = conn_metar.cursor()

        # Get all fetch dates with data
        cn.execute("""
            SELECT DISTINCT target_date FROM nwp_forecasts
            WHERE variable=?
            ORDER BY target_date ASC
        """, (HIGH_TEMP_VAR,))
        all_dates = [r[0] for r in cn.fetchall()]

        # Filter by date range if provided
        if start_date:
            all_dates = [d for d in all_dates if d >= start_date]
        if end_date:
            all_dates = [d for d in all_dates if d <= end_date]

        if not all_dates:
            print("No NWP data available for backtest.")
            return

        print(f"Backtest period: {all_dates[0]} to {all_dates[-1]} ({len(all_dates)} dates)")
        print(f"Stations: {len(STATIONS)}")
        print()

        # For each station, evaluate gate and compare to settlement
        total_signals = 0
        correct_signals = 0
        total_up = 0
        correct_up = 0
        total_down = 0
        correct_down = 0
        flat_count = 0
        per_station = {}

        for station in STATIONS:
            st_signals = 0
            st_correct = 0
            st_flat = 0

            for target_date in all_dates:
                result = self.evaluate(station, target_date)
                if result['prev_actual'] is None:
                    continue

                # Get actual settlement direction
                td = datetime.strptime(target_date, '%Y-%m-%d')
                prev_date = (td - timedelta(days=1)).strftime('%Y-%m-%d')
                cm.execute("""
                    SELECT settlement_bucket, prior_settlement_bucket
                    FROM settlement_epochs
                    WHERE station=? AND market_type='HIGH'
                    AND local_trading_date=?
                    AND epoch_status='closed'
                    AND prior_settlement_bucket IS NOT NULL
                """, (station, target_date))
                row = cm.fetchone()
                if row is None:
                    continue

                actual_direction = 'up' if row[0] > row[1] else 'down'

                if result['direction'] is None:
                    st_flat += 1
                    flat_count += 1
                    continue

                st_signals += 1
                total_signals += 1

                if result['direction'] == actual_direction:
                    st_correct += 1
                    correct_signals += 1
                    if result['direction'] == 'up':
                        total_up += 1
                        correct_up += 1
                    else:
                        total_down += 1
                        correct_down += 1
                else:
                    if result['direction'] == 'up':
                        total_up += 1
                    else:
                        total_down += 1

            st_accuracy = st_correct / st_signals if st_signals > 0 else 0
            per_station[station] = {
                'signals': st_signals,
                'correct': st_correct,
                'flat': st_flat,
                'accuracy': st_accuracy
            }

        # Print results
        print(f"{'Station':<8} {'Signals':>8} {'Correct':>8} {'Accuracy':>10} {'Flat':>8}")
        print("-" * 50)
        for station in sorted(per_station.keys()):
            s = per_station[station]
            print(f"{station:<8} {s['signals']:>8} {s['correct']:>8} "
                  f"{s['accuracy']:>10.2%} {s['flat']:>8}")

        overall_acc = correct_signals / total_signals if total_signals > 0 else 0
        coverage = total_signals / (total_signals + flat_count) if (total_signals + flat_count) > 0 else 0

        print(f"\n{'AGGREGATE':<8} {total_signals:>8} {correct_signals:>8} "
              f"{overall_acc:>10.2%} {flat_count:>8}")
        print(f"Coverage: {coverage:.1%}  (flat/non-signal: {flat_count})")

        if total_up > 0 or total_down > 0:
            up_acc = correct_up / total_up if total_up > 0 else 0
            down_acc = correct_down / total_down if total_down > 0 else 0
            print(f"UP: {correct_up}/{total_up} = {up_acc:.2%}  |  "
                  f"DOWN: {correct_down}/{total_down} = {down_acc:.2%}")

        if total_signals > 30:
            z = (overall_acc - 0.5) * math.sqrt(total_signals) / math.sqrt(0.25)
            print(f"Binomial z-score: {z:.3f}")

        conn_nwp.close()
        conn_metar.close()

        return {
            'total_signals': total_signals,
            'correct': correct_signals,
            'accuracy': overall_acc,
            'flat': flat_count,
            'coverage': coverage,
            'per_station': per_station,
        }


# ─── CLI ────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    import sys

    gate = EnsembleAgreementGate()

    if len(sys.argv) > 1 and sys.argv[1] == 'backtest':
        gate.backtest()
    elif len(sys.argv) > 1 and sys.argv[1] == 'today':
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        results = gate.evaluate_all_stations(today)
        print(f"=== Ensemble Agreement Gate — {today} ===")
        print(f"{'Station':<8} {'Direction':>10} {'Agreement':>10} "
              f"{'Up':>4} {'Down':>5} {'Flat':>5} {'Prev':>8}")
        print("-" * 65)
        for station in sorted(results.keys()):
            r = results[station]
            dir_str = r['direction'] or 'FLAT'
            print(f"{station:<8} {dir_str:>10} {str(r['agreement'] or '-'):>10} "
                  f"{r['up_count']:>4} {r['down_count']:>5} {r['flat_count']:>5} "
                  f"{r['prev_actual'] or '-':>8}")
    else:
        print("Usage:")
        print("  python ensemble_agreement.py backtest   — Run historical backtest")
        print("  python ensemble_agreement.py today       — Evaluate all stations for today")
        print("  python ensemble_agreement.py <station> <date>  — Single evaluation")
