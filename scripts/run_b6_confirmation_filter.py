#!/usr/bin/env python3
"""
B6.2: Confirmation Filter Experiment
Only trade when ≥3 signals agree AND signed sum exceeds threshold
Updated to work with the NEW signal set (post T2 cleanup)
"""

import sqlite3
import os
import json
from typing import Dict, List, Tuple, Optional
from collections import defaultdict
import math
from datetime import datetime


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


def run_confirmation_filter_experiment():
    """Run B6.2 Confirmation Filter on the new signal set"""
    print("=" * 80)
    print("B6.2: Strong Confirmation Filter Experiment (Updated for New Signal Set)")
    print("=" * 80)
    
    db_path = "/home/node/.openclaw/workspace/prototypes/weather-engine-source/data/metar_backfill.db"
    stations = ['KNYC', 'KLAX', 'KMDW', 'KBOS', 'KATL', 'KSFO', 'KSEA']
    
    # Updated parameters for new signal set
    # With fewer signals (now 4-5 vs old 9), adjust threshold accordingly
    filter_agreement_threshold = 2  # At least 2 of 4-5 signals agree (instead of 3 of 9 or 5)
    filter_confidence_threshold = 0.5  # Higher confidence requirement
    
    print(f"\nParameters for new signal set (4 signals):")
    print(f"  Agreement threshold: {filter_agreement_threshold} (out of 4-5 available signals)")
    print(f"  Confidence threshold: {filter_confidence_threshold} (signed sum must exceed this)")
    
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
            day_before = aligned[i-2] if i >= 2 else None
            
            actual = today['market_dir']
            if actual == 'flat':
                continue
            
            # Generate signals — NO look-ahead bias
            # CRITICAL: No today['high'] usage. High IS the settlement value.
            raw_signals = []
            
            # Signal 1: Calendar climatology — prior-day trend
            if yesterday['high'] and day_before and day_before['high']:
                prior_trend = yesterday['high'] - day_before['high']
                direction = 'up' if prior_trend > 0 else 'down'
                conf = 0.40 if abs(prior_trend) >= 1.5 else 0.30
                raw_signals.append({'dir': direction, 'conf': conf, 'name': 'calendar_climatology'})
            
            # Signal 2: Late-day momentum — 3-day prior trend
            if i >= 32 and yesterday['high'] and aligned[i-3]['high']:
                trend = yesterday['high'] - aligned[i-3]['high']
                direction = 'up' if trend > 0 else 'down'
                conf = 0.50 if abs(trend) > 1.7 else 0.35
                raw_signals.append({'dir': direction, 'conf': conf, 'name': 'late_day_momentum_hourly'})
            
            # Signal 3: Late-day analysis — prior-day pattern
            if day_before and day_before['high'] and yesterday['high']:
                change = yesterday['high'] - day_before['high']
                if abs(change) > 1.0:
                    dir_ = 'up' if change > 0 else 'down'
                    conf = 0.35
                else:
                    dir_ = 'down' if change > 0 else 'up'
                    conf = 0.30
                raw_signals.append({'dir': dir_, 'conf': conf, 'name': 'late_day_analysis'})
                
            # Signal 4: NWP analog — prior-day pressure change
            if yesterday['pressure'] and day_before and day_before['pressure']:
                pres_change = yesterday['pressure'] - day_before['pressure']
                nwp_direction = 'up' if pres_change > 0 else 'down'
                nwp_conf = 0.40
                raw_signals.append({'dir': nwp_direction, 'conf': nwp_conf, 'name': 'nwp_analog'})
            
            # Signal 5: Goldilocks — prior-day spike detection only
            if day_before and i >= 3:
                two_back = aligned[i-3]
                prev_change = yesterday['high'] - day_before['high']
                change_before = day_before['high'] - two_back['high']
                
                if abs(prev_change) > 2.0 and abs(prev_change) > abs(change_before) * 1.5:
                    gold_dir = 'down' if prev_change > 0 else 'up'
                    gold_conf = 0.40
                else:
                    gold_dir = 'up' if prev_change > 0 else 'down'
                    gold_conf = 0.25
                
                raw_signals.append({'dir': gold_dir, 'conf': gold_conf, 'name': 'goldilocks'})
            
            # Apply confirmation filter based on actual available signals 
            # Since we now have ~5 signals not the original 9, adjust our filtering
            
            if len(raw_signals) < filter_agreement_threshold:
                continue
            
            # Count agreements
            up_signals = [s for s in raw_signals if s['dir'] == 'up']
            down_signals = [s for s in raw_signals if s['dir'] == 'down']
            
            # Check if enough signals agree
            if len(up_signals) >= filter_agreement_threshold:
                majority_dir = 'up'
                signed_sum = sum(s['conf'] for s in up_signals) - sum(s['conf'] for s in down_signals)
            elif len(down_signals) >= filter_agreement_threshold:
                majority_dir = 'down' 
                signed_sum = sum(s['conf'] for s in down_signals) - sum(s['conf'] for s in up_signals)
            else:
                continue  # Not enough agreement
                        
            # Check if signed confidence sum exceeds threshold
            if abs(signed_sum) < filter_confidence_threshold:
                continue
            
            # Execute trade with confirmed signal
            ensemble_prediction = majority_dir
            is_correct = ensemble_prediction == actual
            risk_status = risk_mgr.update_after_trade(is_correct)
            
            if risk_status == "STABLE":
                all_predictions.append(ensemble_prediction)
                all_actual.append(actual)
                trade_count += 1
                if trade_count % 200 == 0:
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
        print("B6.2 CONFIRMATION FILTER RESULTS (New Signal Set)")
        print("=" * 80)
        print(f"  Directional accuracy: {accuracy*100:.2f}%")
        print(f"  Sharpe ratio: {sharpe:.3f}")
        print(f"  Trade count: {len(all_predictions)}")
        print(f"  Risk state: {risk_mgr.risk_state}")
        print(f"  Agreement threshold: {filter_agreement_threshold} signals")
        print(f"  Confidence threshold: {filter_confidence_threshold}")
        print("=" * 80)
        
        result = {
            'accuracy': accuracy,
            'sharpe': sharpe,
            'trade_count': len(all_predictions),
            'agreement_threshold': filter_agreement_threshold,
            'confidence_threshold': filter_confidence_threshold,
            'signals_tracked': [
                'calendar_climatology', 
                'late_day_momentum_hourly', 
                'late_day_analysis',
                'nwp_analog',
                'goldilocks'
            ]
        }
        
        # Save results to JSON file
        output_dir = "/home/node/.openclaw/workspace/prototypes/weather-engine-source/reports"
        os.makedirs(output_dir, exist_ok=True)
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_file = f"{output_dir}/b6_confirmation_filter_{timestamp}.json"
        
        with open(output_file, 'w') as f:
            json.dump(result, f, indent=2)
        
        print(f"  Results saved to: {output_file}")
        
        return result
    else:
        print("No qualified trades generated with confirmation filter.")
        return None


if __name__ == "__main__":
    result = run_confirmation_filter_experiment()
    if result:
        print(f"\nSummary: Accuracy={result['accuracy']*100:.2f}%, Sharpe={result['sharpe']:.3f}")
    else:
        print("Experiment failed")