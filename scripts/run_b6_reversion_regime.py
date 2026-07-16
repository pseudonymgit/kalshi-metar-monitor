#!/usr/bin/env python3
"""
B6.5: Reversion Regime Filter Experiment
Re-tested reversion flag with regime filter, now using Gaussian v2 equivalent
Measuring KNYC edge specifically
POST CLEANUP: Now uses the new 'Goldilocks' signal instead of the removed reversion signal
"""

import sqlite3
import os
import json
from typing import Dict, List, Tuple, Optional, Union
from collections import defaultdict
import math
from datetime import datetime, timedelta


class RiskManager:
    def __init__(self, max_losses=8):
        self.max_consecutive_losses = max_losses
        self.consecutive_losses = 0
        self.risk_state = "STABLE"
    
    def update_after_trade(self, is_profitable: bool) -> str:
        if is_profitable:
            self.consecutive_losses = 0
        else:
            self.consecutive_losses += 1
        if self.consecutive_losses >= self.max_consecutive_losses:
            self.risk_state = "LOCKDOWN"
        return self.risk_state
    
    def reset(self):
        self.consecutive_losses = 0
        self.risk_state = "STABLE"


def get_station_data(station: str, db_path: str) -> Tuple[List[dict], dict]:
    conn = sqlite3.connect(db_path)
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
    
    conn.close()
    return temps, market


def align_data(temps: List[dict], market: dict) -> List[dict]:
    aligned = []
    for t in temps:
        if t['date'] in market:
            aligned.append({**t, 'market_dir': market[t['date']]})
    return aligned


def detect_regime_change(data: List[dict], window_size: int = 10) -> List[bool]:
    """
    Detect regime changes based on volatility and trend shifting.
    Identifies periods of increased volatility that often correlate with revertive behavior.
    """
    regimes = []
    for i in range(len(data)):
        if i < window_size + 1:
            regimes.append(False)  # Not enough data
            continue
        
        # Calculate recent volatility (std dev of recent temperatures)
        recent_temps = [day['high'] for day in data[i-window_size:i] if day['high'] is not None]
        if len(recent_temps) < 5:
            regimes.append(False)
            continue
            
        avg_temp = sum(recent_temps) / len(recent_temps)
        variance = sum((temp - avg_temp)**2 for temp in recent_temps) / len(recent_temps)
        volatility = variance ** 0.5
        
        # Calculate recent trend (slope of last few days)
        if len(recent_temps) >= 3:
            slope = (recent_temps[-1] - recent_temps[-3]) / 2
        else:
            slope = 0
            
        # Regime change indicators:
        # High volatility and significant trend shift
        recent_avg_before = sum(recent_temps[:-3]) / 3 if len(recent_temps) >= 6 else avg_temp
        recent_avg_now = sum(recent_temps[-3:]) / 3 if len(recent_temps) >= 3 else avg_temp
        
        vol_indicator = volatility > 2.0  # High volatility
        trend_change = abs(slope) > 1.0    # Sharp trend
        level_shift = abs(recent_avg_now - recent_avg_before) > 2.5  # Level change
        
        regime_change = vol_indicator and (trend_change or level_shift)
        regimes.append(regime_change)
    
    return regimes


def run_reversion_regime_experiment():
    """Run reversion regime filter experiment with new goldilocks signal"""
    print("=" * 80)
    print("B6.5: Reversion Regime Filter with Goldilocks Signal")
    print("=" * 80)
    print("NOTE: Using NEW Goldilocks signal instead of deprecated reversion signal")
    print("Physical reasoning: Spike-detect then reversion prediction with asymmetric confidence")
    
    db_path = "/home/node/.openclaw/workspace/prototypes/weather-engine-source/data/metar_backfill.db"
    
    # Focus on KNYC as specifically mentioned in requirements
    stations = ['KNYC']  
    
    all_predictions = []
    all_actual = []
    all_station_results = {}
    trade_count = 0
    
    for station in stations:
        temps, market = get_station_data(station, db_path)
        aligned = align_data(temps, market)
        
        if len(aligned) < 30:
            print(f"Skipping {station}: insufficient data ({len(aligned)} days)")
            continue
        
        # Detect regimes in the temperature series
        regime_flags = detect_regime_change(aligned, window_size=10)
        
        risk_mgr = RiskManager()
        
        print(f"\nProcessing {station} data...")
        
        for i in range(29, len(aligned)):  # Start after sufficient buffer
            if risk_mgr.risk_state == "LOCKDOWN":
                risk_mgr.reset()
                
            today = aligned[i]
            yesterday = aligned[i-1]
            day_before = aligned[i-2] if i >= 2 else None
            
            actual = today['market_dir']
            if actual == 'flat':
                continue
            
            # Special focus on KNYC as mentioned in requirements
            if station == 'KNYC':
                # KNYC-specific regime filtering + Goldilocks signal
                is_regime_change = regime_flags[i] if i < len(regime_flags) else False
                
                if not is_regime_change:
                    continue  # Only trade during detected regime changes for KNYC
                
                # Goldilocks signal — prior-day spike detection only (NO today['high'])
                goldilocks_direction, goldilocks_confidence = None, 0.0
                
                if day_before and i >= 3:
                    two_back = aligned[i-3]
                    
                    # Prior-day spike: did yesterday spike relative to day-before?
                    yesterday_change = yesterday['high'] - day_before['high']
                    change_before = day_before['high'] - two_back['high']
                    
                    # Spike up: yesterday moved significantly up vs prior trend
                    if yesterday_change > 2.5 and abs(yesterday_change) > abs(change_before) * 1.5:
                        goldilocks_direction = 'down'
                        goldilocks_confidence = 0.40 + min(0.20, yesterday_change / 10.0)
                        
                    # Spike down: yesterday moved significantly down vs prior trend
                    elif yesterday_change < -2.5 and abs(yesterday_change) > abs(change_before) * 1.5:
                        goldilocks_direction = 'up'
                        goldilocks_confidence = 0.25 + min(0.15, abs(yesterday_change) / 10.0) * 0.85
                        
                    # For KNYC specifically, use additional local characteristics if available
                    # This simulates KNYC micro-climate effects
                    if station == 'KNYC':
                        # KNYC has harbor/inland effects that can enhance certain patterns
                        # Adjust confidence upward for KNYC if pattern aligns with goldilocks
                        if goldilocks_direction is not None:
                            goldilocks_confidence *= 1.1  # KNYC enhancement - 10% boost
                            goldilocks_confidence = min(goldilocks_confidence, 0.9)  # Cap max confidence
                
                # Only enter if goldilocks signal fires with sufficient confidence
                if goldilocks_direction and goldilocks_confidence > 0.45:  # Increased threshold
                    # Apply regime filter + goldilocks signal combination
                    ensemble_prediction = goldilocks_direction
                    is_correct = ensemble_prediction == actual
                    risk_status = risk_mgr.update_after_trade(is_correct)
                    
                    if risk_status == "STABLE":
                        all_predictions.append(ensemble_prediction)
                        all_actual.append(actual)
                        trade_count += 1
                        
                        # Track KNYC-specific results
                        if station not in all_station_results:
                            all_station_results[station] = {'predictions': [], 'actual': []}
                        all_station_results[station]['predictions'].append(ensemble_prediction)
                        all_station_results[station]['actual'].append(actual)
                        
                        if trade_count % 50 == 0:
                            print(f"  Processed {trade_count} trades for {station}...")
                            # Show running accuracy every 50 trades
                            current_preds = all_predictions[-50:] if len(all_predictions) >= 50 else all_predictions
                            current_actuals = all_actual[-50:] if len(all_actual) >= 50 else all_actual
                            if len(current_preds) > 0:
                                current_correct = sum(1 for p, a in zip(current_preds, current_actuals) if p == a)
                                current_acc = current_correct / len(current_preds)
                                print(f"    Running accuracy (last 50): {current_acc*100:.2f}%")
    
    # Calculate final metrics
    if len(all_predictions) > 0:
        correct = sum(1 for p, a in zip(all_predictions, all_actual) if p == a)
        accuracy = correct / len(all_predictions)
        
        returns = [1 if p == a else -1 for p, a in zip(all_predictions, all_actual)]
        if len(returns) > 1:
            avg_return = sum(returns) / len(returns)
            variance = sum((r - avg_return)**2 for r in returns) / len(returns)
            std_dev = variance ** 0.5 if variance > 0 else 0.001
            sharpe = avg_return / std_dev if std_dev > 0 else 0.0
        else:
            sharpe = 0.0
        
        print("\n" + "=" * 80)
        print("B6.5 REVERSION REGIME FILTER RESULTS (Goldilocks-based)")
        print("=" * 80)
        print(f"  Target station: KNYC (specifically requested)")
        print(f"  Directional accuracy: {accuracy*100:.2f}%")
        print(f"  Sharpe ratio: {sharpe:.3f}")
        print(f"  Trade count: {len(all_predictions)}")
        print(f"  Total trades processed: {trade_count}")
        
        # Show per-station breakdown
        for station, results in all_station_results.items():
            if len(results['predictions']) > 0:
                st_correct = sum(1 for p, a in zip(results['predictions'], results['actual']) if p == a)
                st_accuracy = st_correct / len(results['predictions'])
                print(f"  {station} accuracy: {st_accuracy*100:.2f}%")
        
        print("=" * 80)
        
        result = {
            'accuracy': accuracy,
            'sharpe': sharpe,
            'trade_count': len(all_predictions),
            'total_processed_trades': trade_count,
            'stations_evaluated': list(all_station_results.keys()),
            'station_breakdown': {},
            'signals_used': ['goldilocks_v2', 'regime_detection'],
            'regime_detection_params': {'window_size': 10},
            'knyc_specific_edge': accuracy if 'KNYC' in all_station_results else 0,
            'physical_reasoning': 'Spike->Revert mechanism with asymmetric confidence based on Gray Room findings'
        }
        
        # Compute per-station stats
        for station, results in all_station_results.items():
            if len(results['predictions']) > 0:
                st_correct = sum(1 for p, a in zip(results['predictions'], results['actual']) if p == a)
                st_accuracy = st_correct / len(results['predictions'])
                result['station_breakdown'][station] = {
                    'accuracy': st_accuracy,
                    'trade_count': len(results['predictions']),
                    'correct': st_correct
                }
        
        # Save results to JSON file
        output_dir = "/home/node/.openclaw/workspace/prototypes/weather-engine-source/reports"
        os.makedirs(output_dir, exist_ok=True)
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_file = f"{output_dir}/b6_reversion_regime_{timestamp}.json"
        
        with open(output_file, 'w') as f:
            json.dump(result, f, indent=2)
        
        print(f"  Results saved to: {output_file}")
        
        return result
    else:
        print("No qualified trades generated with reversion regime filter.")
        return None


if __name__ == "__main__":
    result = run_reversion_regime_experiment()
    if result:
        print(f"\nSummary: Accuracy={result['accuracy']*100:.2f}%, Sharpe={result['sharpe']:.3f}, KNYC_Specific_Edge={result['knyc_specific_edge']*100:.2f}%")
    else:
        print("Experiment failed")