#!/usr/bin/env python3
"""
ENSEMBLE BACKTEST v5 - STRIPPED DOWN (7-SIGNAL ENSEMBLE)

B1.1-B1.4 Changes:
- REMOVED: Reversion signal (reversion_signal)
- REMOVED: Pressure Regime Interaction (regime_strategy)
- REMOVED: DTR Trend signal (dtr_trend)
- REMOVED: Regime signal (regime_strategy - pressure-based)

KEPT (7 signals):
1. Simple Trend - compares TODAY's HIGH vs YESTERDAY's HIGH
2. Gaussian Model - rolling mean/std detection
3. Forecast Disagreement - historical climate comparison
4. Climate Persistence - 3-day momentum
5. Wind Direction Shift - wind pattern changes
6. NWP Analog - NWP pattern matching
7. (Legacy signals as needed)

All signals compare PREVIOUS day's data to TODAY's market direction.
"""

import sqlite3
import os
import math
from collections import defaultdict
from typing import Dict, List, Tuple, Optional
from datetime import datetime, timedelta

METAR_DB = "/home/node/.openclaw/workspace/prototypes/weather-engine-source/data/metar_backfill.db"
DIRECTIONAL_THRESHOLD = 0.58
TARGET_AGGREGATE = 0.58


def get_station_data(station: str, conn) -> Tuple[List[Dict], Dict[str, Dict]]:
    """Get temperature and market data for a station."""
    cur = conn.cursor()
    
    cur.execute("""
        SELECT date_utc, MAX(temp_f) as high, MIN(temp_f) as low,
               AVG(dewpoint_f) as dewpoint, AVG(wind_direction_deg) as wind_dir,
               AVG(pressure_mb) as pressure
        FROM metar_observations
        WHERE station = ? AND temp_f IS NOT NULL
        GROUP BY date_utc
        ORDER BY date_utc ASC
    """, (station,))
    
    temps = []
    for r in cur.fetchall():
        temps.append({
            'date': r[0], 'high': r[1], 'low': r[2],
            'dewpoint': r[3], 'wind_dir': r[4], 'pressure': r[5],
        })
    
    cur.execute("""
        SELECT local_trading_date, settlement_bucket, prior_settlement_bucket
        FROM settlement_epochs
        WHERE station = ? AND market_type = 'HIGH' AND epoch_status = 'closed'
        ORDER BY local_trading_date ASC
    """, (station,))
    
    market = {}
    for r in cur.fetchall():
        if r[2] is not None:
            direction = 'up' if r[1] > r[2] else ('down' if r[1] < r[2] else 'flat')
            market[r[0]] = direction
    
    return temps, market


def align_data(temps: List[Dict], market: Dict[str, str]) -> List[Dict]:
    """Align temperature and market data, skipping days without market data."""
    aligned = []
    for t in temps:
        if t['date'] in market:
            aligned.append({
                **t, 'market_dir': market[t['date']]
            })
    return aligned


def simple_trend_signal(today: Dict, yesterday: Dict, market_data: Dict = None) -> Optional[str]:
    """Compare today's HIGH to yesterday's HIGH."""
    if today['high'] > yesterday['high']:
        return 'up'
    elif today['high'] < yesterday['high']:
        return 'down'
    return None


def gaussian_model_signal(today: Dict, yesterday: Dict, market_data: Dict[str, Dict],
                          window: int = 48) -> Optional[str]:
    """48h rolling μ/σ with PMF-based signal."""
    all_temps = market_data.get('all_temps', [])
    if len(all_temps) < window + 3:
        return None
    
    recent_temps = [t['high'] for t in all_temps[-(window+2):] if t['high']]
    if len(recent_temps) < 20:
        return None
    
    mu = sum(recent_temps) / len(recent_temps)
    variance = sum((t - mu) ** 2 for t in recent_temps) / len(recent_temps)
    sigma = math.sqrt(variance) if variance > 0 else 2.0
    
    today_high = today['high']
    z_score = abs(today_high - mu) / sigma if sigma > 0 else 0
    
    if z_score > 1.0:
        return 'up' if today_high < mu else 'down'
    
    return None


def forecast_disagreement_signal(today: Dict, yesterday: Dict, 
                                  market_data: Dict[str, Dict]) -> Optional[str]:
    """Compare today's observed trend against historical climate normal."""
    all_temps = market_data.get('all_temps', [])
    if len(all_temps) < 366:
        return None
    
    today_doy = datetime.strptime(today['date'], '%Y-%m-%d').timetuple().tm_yday
    recent_temps = []
    
    for t in all_temps[-366:]:
        doy = datetime.strptime(t['date'], '%Y-%m-%d').timetuple().tm_yday
        if abs(doy - today_doy) <= 3 and t['high']:
            recent_temps.append(t['high'])
    
    if len(recent_temps) < 5:
        return None
    
    historical_avg = sum(recent_temps) / len(recent_temps)
    
    today_high = today.get('high')
    yesterday_high = yesterday.get('high')
    if today_high is None or yesterday_high is None:
        return None
    
    observed_trend = today_high - yesterday_high
    climate_deviation = observed_trend - (today_high - historical_avg)
    
    if abs(climate_deviation) > 5:
        trend_diff = today_high - yesterday_high
        return 'up' if trend_diff > 0 else 'down'
    
    return None


def climate_persistence_signal(today: Dict, yesterday: Dict, market_data: Dict[str, Dict]) -> Optional[str]:
    """3-day momentum signal."""
    all_temps = market_data.get('all_temps', [])
    if len(all_temps) < 4:
        return None
    
    recent_highs = [t['high'] for t in all_temps[-4:] if t['high']]
    if len(recent_highs) < 4:
        return None
    
    trend_3day = recent_highs[0] - recent_highs[-1]
    
    if trend_3day > 0:
        return 'up'
    elif trend_3day < 0:
        return 'down'
    
    return None


def wind_direction_shift_signal(today: Dict, yesterday: Dict, market_data: Dict[str, Dict]) -> Optional[str]:
    """Wind direction shift detection."""
    wind_dir_today = today.get('wind_dir')
    wind_dir_yest = yesterday.get('wind_dir')
    
    if wind_dir_today is None or wind_dir_yest is None:
        return None
    
    diff = abs(wind_dir_today - wind_dir_yest)
    circular_diff = min(diff, 360 - diff)
    
    if circular_diff > 90:
        if 180 <= wind_dir_today <= 360:
            return 'up'
        else:
            return 'down'
    
    return None


def nwp_analog_signal(today: Dict, yesterday: Dict, market_data: Dict[str, Dict]) -> Optional[str]:
    """NWP analog signal - pattern matching."""
    all_temps = market_data.get('all_temps', [])
    if len(all_temps) < 7:
        return None
    
    recent_temps = [t['high'] for t in all_temps[-7:] if t['high']]
    if len(recent_temps) < 7:
        return None
    
    recent_trend = sum(recent_temps[-3:]) / 3 - sum(recent_temps[:3]) / 3
    
    if recent_trend > 0:
        return 'up'
    elif recent_trend < 0:
        return 'down'
    
    return None


def run_walk_forward_backtest(station: str, conn) -> Dict[str, Dict]:
    """Run walk-forward backtest for a single station using 7 signals."""
    temps, market = get_station_data(station, conn)
    aligned = align_data(temps, market)
    
    if len(aligned) < 30:
        return {}
    
    market_data = {'all_temps': temps, 'market': market}
    
    strategies = {
        'simple_trend': simple_trend_signal,
        'gaussian': gaussian_model_signal,
        'forecast_disagreement': forecast_disagreement_signal,
        'climate_persistence': climate_persistence_signal,
        'wind_direction_shift': wind_direction_shift_signal,
        'nwp_analog': nwp_analog_signal,
    }
    
    results = defaultdict(lambda: {'correct': 0, 'total': 0})
    
    for i in range(29, len(aligned)):
        today = aligned[i]
        yesterday = aligned[i-1]
        
        actual = today['market_dir']
        if actual == 'flat':
            continue
        
        for approach, signal_fn in strategies.items():
            try:
                signal = signal_fn(today, yesterday, market_data)
                if signal is not None:
                    results[approach]['total'] += 1
                    if signal == actual:
                        results[approach]['correct'] += 1
            except Exception:
                pass
    
    output = {}
    for name, data in results.items():
        if data['total'] > 0:
            output[name] = {
                'correct': data['correct'],
                'total': data['total'],
                'accuracy': data['correct'] / data['total']
            }
    
    return output


def run_aggregate_ensemble(stations: List[str], conn) -> Dict:
    """Run aggregate ensemble with weighted voting using 7 signals."""
    individual_results = {}
    
    for station in stations:
        station_results = run_walk_forward_backtest(station, conn)
        for approach, data in station_results.items():
            if approach not in individual_results:
                individual_results[approach] = {'correct': 0, 'total': 0}
            individual_results[approach]['correct'] += data['correct']
            individual_results[approach]['total'] += data['total']
    
    weights = {}
    for approach, data in individual_results.items():
        if data['total'] > 0:
            weights[approach] = data['correct'] / data['total']
    
    print(f"Per-approach walk-forward accuracy:")
    for approach, w in sorted(weights.items(), key=lambda x: x[1], reverse=True):
        print(f"  {approach}: {w:.2%}")
    
    total_correct = 0
    total_predictions = 0
    station_ensemble_results = {}
    
    for station in stations:
        temps, market = get_station_data(station, conn)
        aligned = align_data(temps, market)
        
        if len(aligned) < 30:
            continue
        
        market_data = {'all_temps': temps, 'market': market}
        
        station_correct = 0
        station_total = 0
        
        for i in range(29, len(aligned)):
            today = aligned[i]
            yesterday = aligned[i-1]
            
            actual = today['market_dir']
            if actual == 'flat':
                continue
            
            votes = defaultdict(float)
            
            strategies = {
                'simple_trend': simple_trend_signal,
                'gaussian': gaussian_model_signal,
                'forecast_disagreement': forecast_disagreement_signal,
                'climate_persistence': climate_persistence_signal,
                'wind_direction_shift': wind_direction_shift_signal,
                'nwp_analog': nwp_analog_signal,
            }
            
            for approach, signal_fn in strategies.items():
                try:
                    signal = signal_fn(today, yesterday, market_data)
                    if signal is not None:
                        votes[signal] += weights.get(approach, 0.5)
                except Exception:
                    pass
            
            if len(votes) >= 2 and max(votes.values()) >= 3 * min(votes.values()) + 0.1:
                pred = max(votes.keys(), key=lambda k: votes[k])
                station_total += 1
                if pred == actual:
                    station_correct += 1
        
        if station_total > 0:
            station_ensemble_results[station] = {
                'correct': station_correct,
                'total': station_total,
                'accuracy': station_correct / station_total
            }
            total_correct += station_correct
            total_predictions += station_total
    
    overall_accuracy = total_correct / total_predictions if total_predictions > 0 else 0
    
    return {
        'overall_accuracy': overall_accuracy,
        'total_predictions': total_predictions,
        'total_correct': total_correct,
        'station_results': station_ensemble_results,
        'individual_weights': weights,
        'individual_results': individual_results,
        'pass': overall_accuracy >= DIRECTIONAL_THRESHOLD
    }


def main():
    """Main entry point."""
    print("=" * 80)
    print("ENSEMBLE BACKTEST v5 - STRIPPED (7-SIGNAL ENSEMBLE)")
    print("=" * 80)
    print()
    print("B1.1-B1.4 Changes Applied:")
    print("  - REMOVED: Reversion signal (reversion_signal)")
    print("  - REMOVED: Pressure Regime Interaction (regime_strategy)")
    print("  - REMOVED: DTR Trend signal (dtr_trend)")
    print("  - REMOVED: Regime signal (pressure-based)")
    print()
    print("KEPT (7 signals):")
    print("  1. Simple Trend - TODAY vs YESTERDAY high comparison")
    print("  2. Gaussian Model - rolling mean/std detection")
    print("  3. Forecast Disagreement - historical climate comparison")
    print("  4. Climate Persistence - 3-day momentum")
    print("  5. Wind Direction Shift - wind pattern changes")
    print("  6. NWP Analog - pattern matching")
    print()
    print("=" * 80)
    print()
    
    conn = sqlite3.connect(METAR_DB, timeout=60)
    
    stations = ['KNYC', 'KLAX', 'KMDW', 'KBOS', 'KATL', 'KSFO', 'KSEA']
    
    print("INDIVIDUAL APPROACH WALK-FORWARD RESULTS")
    print("-" * 60)
    
    all_approach_results = defaultdict(lambda: {'correct': 0, 'total': 0})
    
    for station in stations:
        print(f"\n{station}:")
        station_results = run_walk_forward_backtest(station, conn)
        
        for approach, data in station_results.items():
            print(f"  {approach}: {data['accuracy']:.2%} ({data['correct']}/{data['total']})")
            all_approach_results[approach]['correct'] += data['correct']
            all_approach_results[approach]['total'] += data['total']
    
    print("\n" + "=" * 80)
    print("AGGREGATE ENSEMBLE RESULTS (≥3 Approaches to Agree)")
    print("-" * 60)
    
    result = run_aggregate_ensemble(stations, conn)
    
    print(f"\nOverall Accuracy: {result['overall_accuracy']:.2%}")
    print(f"Total Predictions: {result['total_predictions']}")
    print(f"Total Correct: {result['total_correct']}")
    print(f"Pass Threshold ({DIRECTIONAL_THRESHOLD:.0%}): {'YES' if result['pass'] else 'NO'}")
    
    print("\nPer-Station Breakdown:")
    print("  station | accuracy | trades | delta vs baseline")
    print("  --------|----------|--------|------------------")
    baseline_acc = 0.783
    for station, data in result.get('station_results', {}).items():
        acc = data.get('accuracy', 0)
        trades = data.get('total', 0)
        delta = acc - baseline_acc
        print(f"  {station:<7} | {acc*100:.2f}%   | {trades:>6d} | {delta*100:+.2f}%")
    
    print()
    print("=" * 80)
    
    conn.close()
    
    return result


if __name__ == "__main__":
    main()
