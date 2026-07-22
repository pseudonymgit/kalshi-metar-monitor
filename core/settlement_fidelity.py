#!/usr/bin/env python3

# CHANGELOG (last 10 broad changes):
# 1. [2026-07-05 R4-1.1: Fix P&L mark-to-market + thread-safe price cache]
#

"""
P3: Settlement Fidelity Diagnostic + 5-min Station Bias Correction

Two functions:
1. Diagnose divergence between METAR daily_stats and settlement_epochs
   (tracks how well our observed temperatures match Kalshi's settlement values)
2. Apply small bias correction for stations that report on 5-minute intervals
   (these stations tend to capture slightly different max/min values than
   hourly-reporting stations due to sub-hourly temperature fluctuations)

Usage:
    from core.settlement_fidelity import SettlementFidelity
    sf = SettlementFidelity()
    sf.run_diagnostic()
    corrected = sf.apply_5min_bias_correction(station, temp_f, direction='max')
"""

import math
from datetime import datetime, timedelta
from collections import defaultdict

METAR_DB = "/home/node/.openclaw/workspace/prototypes/weather-engine-source/data/metar_backfill.db"

# Authoritative NWS station list for Kalshi weather markets
# Updated 2026-07-03: KNYC uses Central Park (KNYC), not JFK
# KDAL removed: Kalshi settles Dallas on KDFW
# 5-minute reporting stations identified by observation frequency analysis
AUTHORITATIVE_STATIONS = {
    'KATL': {'city': 'Atlanta',         'state': 'GA', 'lat': 33.6407,  'lon': -84.4277,  'reports_5min': False},
    'KAUS': {'city': 'Austin',           'state': 'TX', 'lat': 30.1945,  'lon': -97.6699, 'reports_5min': False},
    'KBOS': {'city': 'Boston',           'state': 'MA', 'lat': 42.3656,  'lon': -71.0096, 'reports_5min': False},
    'KDCA': {'city': 'Washington DC',    'state': 'DC', 'lat': 38.8512,  'lon': -77.0402, 'reports_5min': False},
    'KDEN': {'city': 'Denver',           'state': 'CO', 'lat': 39.8561,  'lon': -104.6737,'reports_5min': False},
    'KDFW': {'city': 'Dallas-Fort Worth','state': 'TX', 'lat': 32.8998,  'lon': -97.0403, 'reports_5min': False},
    'KHOU': {'city': 'Houston',          'state': 'TX', 'lat': 29.6454,  'lon': -95.2789, 'reports_5min': False},
    'KLAS': {'city': 'Las Vegas',        'state': 'NV', 'lat': 36.0840,  'lon': -115.1537,'reports_5min': False},
    'KLAX': {'city': 'Los Angeles',      'state': 'CA', 'lat': 33.9425,  'lon': -118.4081,'reports_5min': False},
    'KMDW': {'city': 'Chicago',          'state': 'IL', 'lat': 41.7868,  'lon': -87.7522, 'reports_5min': False},
    'KMIA': {'city': 'Miami',            'state': 'FL', 'lat': 25.7959,  'lon': -80.2870, 'reports_5min': False},
    'KMSP': {'city': 'Minneapolis',      'state': 'MN', 'lat': 44.8848,  'lon': -93.2223, 'reports_5min': False},
    'KMSY': {'city': 'New Orleans',     'state': 'LA', 'lat': 29.9934,  'lon': -90.2580, 'reports_5min': False},
    'KNYC': {'city': 'New York',         'state': 'NY', 'lat': 40.7128,  'lon': -74.0060, 'reports_5min': True},  # Central Park
    'KOKC': {'city': 'Oklahoma City',    'state': 'OK', 'lat': 35.3931,  'lon': -97.6007, 'reports_5min': False},
    'KPHL': {'city': 'Philadelphia',     'state': 'PA', 'lat': 39.8744,  'lon': -75.2424, 'reports_5min': False},
    'KPHX': {'city': 'Phoenix',          'state': 'AZ', 'lat': 33.4342,  'lon': -112.0116,'reports_5min': False},
    'KSAT': {'city': 'San Antonio',      'state': 'TX', 'lat': 29.5337,  'lon': -98.4698, 'reports_5min': False},
    'KSEA': {'city': 'Seattle',          'state': 'WA', 'lat': 47.4502,  'lon': -122.3088,'reports_5min': False},
    'KSFO': {'city': 'San Francisco',    'state': 'CA', 'lat': 37.6213,  'lon': -122.3790,'reports_5min': False},
}

# Bias correction for 5-minute stations
# 5-min stations capture more temperature extremes than hourly stations.
# The max tends to be slightly higher and min slightly lower.
# Empirical correction from literature: ~0.3-0.5°F for max, ~-0.3 to -0.5°F for min
FIVE_MIN_MAX_BIAS = 0.4    # Subtract from max temp (it reads slightly high)
FIVE_MIN_MIN_BIAS = -0.4   # Add to min temp (it reads slightly low)


class SettlementFidelity:
    """Diagnose and correct settlement temperature fidelity issues."""

    def __init__(self, db_path=METAR_DB):
        self.db_path = db_path

    def run_diagnostic(self, station=None):
        """
        Compare daily_stats max/min temps against settlement_epochs values.
        Reports divergence metrics: mean bias, RMSE, and per-station breakdown.

        Args:
            station: If None, runs for all stations
        """
        conn = get_sqlite_connection(self.db_path, timeout=10)
        c = conn.cursor()

        stations = [station] if station else list(AUTHORITATIVE_STATIONS.keys())

        print(f"{'Station':<8} {'Days':>6} {'Mean Bias':>10} {'RMSE':>8} "
              f"{'Max Div':>8} {'5-min':>6}")
        print("-" * 55)

        all_divergences = []

        for st in stations:
            # Match daily_stats to settlement_epochs on date
            c.execute("""
                SELECT ds.date_utc, ds.max_temp_f, se.settlement_bucket
                FROM daily_stats ds
                JOIN settlement_epochs se
                ON ds.date_utc = se.local_trading_date
                AND ds.station = se.station
                WHERE ds.station = ? AND se.market_type = 'HIGH'
                AND se.epoch_status = 'closed'
                AND se.settlement_bucket IS NOT NULL
                AND ds.max_temp_f IS NOT NULL
            """, (st,))

            rows = c.fetchall()
            if not rows:
                continue

            biases = []
            max_div = 0
            for date, max_temp, settlement in rows:
                # Settlement bucket is the integer HIGH temp used by Kalshi
                bias = max_temp - settlement
                biases.append(bias)
                max_div = max(max_div, abs(bias))

            n = len(biases)
            mean_bias = sum(biases) / n
            rmse = math.sqrt(sum(b**2 for b in biases) / n)
            is_5min = AUTHORITATIVE_STATIONS.get(st, {}).get('reports_5min', False)

            print(f"{st:<8} {n:>6} {mean_bias:>+10.3f} {rmse:>8.3f} "
                  f"{max_div:>8.1f} {'YES' if is_5min else '':>6}")

            all_divergences.extend([(st, b) for b in biases])

        if all_divergences:
            all_biases = [b for _, b in all_divergences]
            overall_mean = sum(all_biases) / len(all_biases)
            overall_rmse = math.sqrt(sum(b**2 for b in all_biases) / len(all_biases))
            print(f"\n{'OVERALL':<8} {len(all_biases):>6} {overall_mean:>+10.3f} "
                  f"{overall_rmse:>8.3f}")

        conn.close()
        return all_divergences

    def apply_5min_bias_correction(self, station, temp_f, direction='max'):
        """
        Apply small bias correction for 5-minute reporting stations.

        5-minute stations capture sub-hourly temperature extremes, so their
        max temps are slightly inflated and min temps slightly deflated
        relative to hourly-reporting stations.

        Args:
            station: ICAO code
            temp_f: Temperature in Fahrenheit
            direction: 'max' or 'min'

        Returns: corrected temperature (float)
        """
        if station not in AUTHORITATIVE_STATIONS:
            return temp_f

        if not AUTHORITATIVE_STATIONS[station].get('reports_5min', False):
            return temp_f

        if direction == 'max':
            return temp_f - FIVE_MIN_MAX_BIAS
        elif direction == 'min':
            return temp_f - FIVE_MIN_MIN_BIAS  # Subtract negative = add
        return temp_f

    def detect_5min_stations(self):
        """
        Detect which stations report on 5-minute intervals by analyzing
        observation frequency in metar_observations table.

        Returns: dict of {station: avg_interval_minutes}
        """
        conn = get_sqlite_connection(self.db_path, timeout=10)
        c = conn.cursor()

        stations = list(AUTHORITATIVE_STATIONS.keys())
        results = {}

        for st in stations:
            c.execute("""
                SELECT timestamp_utc FROM metar_observations
                WHERE station = ?
                AND timestamp_utc IS NOT NULL
                ORDER BY timestamp_utc ASC
                LIMIT 500
            """, (st,))

            timestamps = [r[0] for r in c.fetchall()]
            if len(timestamps) < 10:
                continue

            # Parse timestamps and compute intervals
            intervals = []
            for i in range(1, len(timestamps)):
                try:
                    t1 = datetime.fromisoformat(timestamps[i-1].replace('Z', '+00:00'))
                    t2 = datetime.fromisoformat(timestamps[i].replace('Z', '+00:00'))
                    diff = (t2 - t1).total_seconds() / 60.0
                    if 0 < diff < 120:  # Ignore gaps > 2 hours
                        intervals.append(diff)
                except Exception:
                    continue

            if intervals:
                avg_interval = sum(intervals) / len(intervals)
                results[st] = round(avg_interval, 1)

                # Flag as 5-min if average interval < 10 minutes
                if avg_interval < 10:
                    AUTHORITATIVE_STATIONS[st]['reports_5min'] = True

        conn.close()

        print("=== Station Observation Intervals ===")
        for st, interval in sorted(results.items()):
            flag = " ← 5-MIN" if interval < 10 else ""
            print(f"  {st}: {interval:.1f} min{flag}")

        return results


if __name__ == '__main__':
    import sys
from .sqlite_utils import get_sqlite_connection, get_readonly_sqlite_connection

    sf = SettlementFidelity()

    if len(sys.argv) > 1 and sys.argv[1] == 'intervals':
        sf.detect_5min_stations()
    elif len(sys.argv) > 1 and sys.argv[1] == 'station' and len(sys.argv) > 2:
        sf.run_diagnostic(station=sys.argv[2])
    else:
        sf.run_diagnostic()
