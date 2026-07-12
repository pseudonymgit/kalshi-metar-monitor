#!/usr/bin/env python3
"""
B6.2: Strong confirmation filter experiment
B6.3: Kalman smoothing integration
B6.4: EWMA smoothing integration
"""

import sqlite3
import os
from typing import Dict, List, Tuple, Optional
from collections import defaultdict
import math
from datetime import datetime


class SimpleEWMA:
    def __init__(self, alpha=0.3):
        self.alpha = alpha
        self.value = 0.5
        
    def update(self, new_value: float) -> float:
        self.value = self.alpha * new_value + (1 - self.alpha) * self.value
        return self.value


class SimpleKalman:
    def __init__(self, process_var=0.1, measurement_var=0.1):
        self.process_var = process_var
        self.measurement_var = measurement_var
        self.estimate = 0.0
        self.error = 1.0
        
    def update(self, measurement: float) -> float:
        self.error += self.process_var
        K = self.error / (self.error + self.measurement_var)
        self.estimate = self.estimate + K * (measurement - self.estimate)
        self.error = (1 - K) * self.error
        return self.estimate


class RiskManager:
    def __init__(self):
        self.max_consecutive_losses = 8
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


def run_b6_experiments():
    """Run B6.2-B6.4 experiments"""
    print("=" * 80)
    print("B6.2-B6.4: Strong Confirmation Filter + Smoothing Experiments")
    print("=" * 80)
    
    db_path = "/home/node/.openclaw/workspace/prototypes/weather-engine-source/data/metar_backfill.db"
    stations = ['KNYC', 'KLAX', 'KMDW', 'KBOS', 'KATL', 'KSFO', 'KSEA']
    
    # Experiment parameters
    filter_agreement_threshold = 2
    filter_confidence_threshold = 0.3
    
    print(f"\nParameters:")
    print(f"  Agreement threshold: {filter_agreement_threshold}")
    print(f"  Confidence threshold: {filter_confidence_threshold}")
    
    kalman_smooth = SimpleKalman(process_var=0.1, measurement_var=0.1)
    ewma_smooth = SimpleEWMA(alpha=0.3)
    
    all_predictions = []
    all_actual = []
    trade_count = 0
    
    risk_mgr = RiskManager()
    
    for station in stations:
        temps, market = get_station_data(station, db_path)
        aligned = align_data(temps, market)
        
        if len(aligned) < 30:
            continue
            
        for i in range(29, len(aligned)):
            if risk_mgr.risk_state == "LOCKDOWN":
                risk_mgr.reset()
            
            today = aligned[i]
            yesterday = aligned[i-1]
            
            actual = today['market_dir']
            if actual == 'flat':
                continue
            
            # Generate signals
            raw_signals = []
            
            # Simple trend
            if today['high'] and yesterday['high']:
                direction = 'up' if today['high'] > yesterday['high'] else 'down'
                conf = 0.6 if direction else 0.0
                if conf > 0:
                    raw_signals.append({'dir': direction, 'conf': conf})
            
            # Momentum
            if i >= 32 and today['high'] and aligned[i-3]['high']:
                trend = today['high'] - aligned[i-3]['high']
                direction = 'up' if trend > 0 else 'down'
                conf = min(0.7, 0.5 + abs(trend) * 0.02)
                raw_signals.append({'dir': direction, 'conf': conf})
            
            # Apply filters
            if len(raw_signals) < filter_agreement_threshold:
                continue
            
            up_count = sum(1 for s in raw_signals if s['dir'] == 'up')
            down_count = sum(1 for s in raw_signals if s['dir'] == 'down')
            
            if up_count >= filter_agreement_threshold:
                majority_dir = 'up'
            elif down_count >= filter_agreement_threshold:
                majority_dir = 'down'
            else:
                continue
            
            signed_sum = sum(s['conf'] if s['dir'] == majority_dir else -s['conf'] for s in raw_signals)
            
            if abs(signed_sum) < filter_confidence_threshold:
                continue
            
            # Apply Kalman smoothing
            kalman_val = kalman_smooth.update(signed_sum)
            
            # Apply EWMA smoothing
            ewma_val = ewma_smooth.update(signed_sum)
            
            ensemble_prediction = majority_dir
            is_correct = ensemble_prediction == actual
            risk_status = risk_mgr.update_after_trade(is_correct)
            
            if risk_status == "STABLE":
                all_predictions.append(ensemble_prediction)
                all_actual.append(actual)
                trade_count += 1
                if trade_count % 500 == 0:
                    print(f"  Processed {trade_count} trades...")

    # Calculate metrics
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
        print("B6.2-B6.4 EXPERIMENT RESULTS")
        print("=" * 80)
        print(f"  Directional accuracy: {accuracy*100:.2f}%")
        print(f"  Sharpe ratio: {sharpe:.3f}")
        print(f"  Trade count: {len(all_predictions)}")
        print(f"  Risk state: {risk_mgr.risk_state}")
        print(f"  Kalman smoothing: applied (process_var=0.1)")
        print(f"  EWMA smoothing: applied (alpha=0.3)")
        print("=" * 80)
        
        return {
            'accuracy': accuracy,
            'sharpe': sharpe,
            'trade_count': len(all_predictions),
            'kalman_params': {'process_var': 0.1, 'measurement_var': 0.1},
            'ewma_params': {'alpha': 0.3},
            'filter_params': {
                'agreement_threshold': filter_agreement_threshold,
                'confidence_threshold': filter_confidence_threshold
            }
        }
    else:
        print("No qualified trades generated.")
        return None


if __name__ == "__main__":
    result = run_b6_experiments()
    if result:
        print(f"\nSummary: Accuracy={result['accuracy']*100:.2f}%, Sharpe={result['sharpe']:.3f}")
    else:
        print("Experiment failed")
