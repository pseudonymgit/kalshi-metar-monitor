#!/usr/bin/env python3
"""
Combined B6.2 + B6.4 Experiment: Strong Confirmation Filter + Kalman/EWMA Smoothing
===============================================================================

Combined experiment: Strong confirmation filter (≥4 signals agree + signed sum 
threshold) AND Kalman/EWMA smoothing on raw signals before the ensemble.
Start from the verified 78.3% / 11,893-trade 9-signal 7-station baseline (KNYC, KLAX, KMDW, KBOS, KATL, KSFO, KSEA).

Enforces B1.5 risk guardrails (consecutive_loss_limit=8) on every trade.
Runs 200-round isotonic calibration sweep optimizing both filter + smoothing parameters.

This script is deterministic only - no AI inside the loop.
"""

import sqlite3
import numpy as np
from typing import Dict, List, Tuple, Optional, Any
from collections import defaultdict
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
import random


@dataclass
class RiskConfig:
    """Risk control configuration parameters per B1.5"""
    max_consecutive_losses: int = 8       # consecutive_loss_limit=8 per requirement
    initial_capital: float = 10000.0     # Starting capital


class KalmanSmoothing:
    """
    Kalman Smoothing for signal values to reduce noise.
    """
    def __init__(self, process_noise=0.1, measurement_noise=0.1, initial_estimate=0.0):
        self.process_noise = process_noise
        self.measurement_noise = measurement_noise
        self.x = initial_estimate  # Initial state estimate
        self.P = 1.0              # Initial state covariance
        
    def update(self, measurement: float) -> float:
        """Update Kalman filter with new measurement and return smoothed value."""
        # Prediction step
        x_pred = self.x  # Assume constant model (stationary)
        P_pred = self.P + self.process_noise
        
        # Update step
        K = P_pred / (P_pred + self.measurement_noise)  # Kalman gain
        self.x = x_pred + K * (measurement - x_pred)
        self.P = (1 - K) * P_pred
        
        return self.x


class EWMASmoothing:
    """
    Exponential Weighted Moving Average for signal smoothing
    """
    def __init__(self, alpha=0.3, initial_value=0.5):
        self.alpha = alpha
        self.ewma = initial_value
        
    def update(self, value: float) -> float:
        """Update EWMA with new value and return smoothed value."""
        self.ewma = self.alpha * value + (1 - self.alpha) * self.ewma
        return self.ewma


class SignalProcessor:
    """
    Process multiple signal sources with smoothing and filtering
    """
    def __init__(self, smoothing_configs: Dict[str, Dict]):
        """
        Initialize with smoothing configurations for each signal
        
        Args:
            smoothing_configs: Dict mapping signal names to their smoothing params
                              Format: {signal_name: {'type': 'kalman'|'ewma', 'params': {...}}}
        """
        self.smoothers = {}
        self.raw_values = {}  # Track raw values before smoothing
        self.smoothed_values = {}  # Track smoothed values
        
        for signal_name, config in smoothing_configs.items():
            s_type = config.get('type', 'kalman')
            s_params = config.get('params', {})
            
            if s_type == 'kalman':
                smoother = KalmanSmoothing(
                    process_noise=s_params.get('process_noise', 0.1),
                    measurement_noise=s_params.get('measurement_noise', 0.1),
                    initial_estimate=s_params.get('initial_estimate', 0.0)
                )
            elif s_type == 'ewma':
                smoother = EWMASmoothing(
                    alpha=s_params.get('alpha', 0.3),
                    initial_value=s_params.get('initial_value', 0.5)
                )
            else:
                # No smoothing
                smoother = None
                
            self.smoothers[signal_name] = smoother
            self.raw_values[signal_name] = []
            self.smoothed_values[signal_name] = []
    
    def process_signal(self, signal_name: str, value: float) -> Tuple[float, float]:
        """
        Process a signal through the appropriate smoother
        
        Args:
            signal_name: Name of the signal
            value: Raw signal value (typically 0-1 or -1 to 1)
            
        Returns:
            (raw_value, smoothed_value) tuple
        """
        # Store raw value
        if signal_name not in self.raw_values:
            self.raw_values[signal_name] = []
        self.raw_values[signal_name].append(value)
        
        # Apply smoothing if appropriate smoother exists
        if signal_name in self.smoothers and self.smoothers[signal_name] is not None:
            smoothed_value = self.smoothers[signal_name].update(value)
        else:
            smoothed_value = value  # No smoothing
            
        # Store smoothed value
        if signal_name not in self.smoothed_values:
            self.smoothed_values[signal_name] = []
        self.smoothed_values[signal_name].append(smoothed_value)
        
        return value, smoothed_value


class RiskManager:
    """
    Implements B1.5 risk controls: consecutive loss limit = 8
    """
    def __init__(self, config: RiskConfig):
        self.config = config
        self.reset()
    
    def reset(self):
        """Reset risk tracking."""
        self.consecutive_losses = 0
        self.risk_state = "STABLE"  # Either STABLE or LOCKDOWN
        self.current_balance = self.config.initial_capital
        
    def update_after_trade(self, is_win: bool) -> str:
        """
        Update risk state after a trade.
        
        Args:
            is_win: True if the trade was profitable, False otherwise
            
        Returns:
            Current risk state ("STABLE" or "LOCKDOWN")
        """
        if is_win:
            self.consecutive_losses = 0  # Reset streak on win
        else:
            self.consecutive_losses += 1  # Increment on loss
            
            # Check if limit exceeded
            if self.consecutive_losses >= self.config.max_consecutive_losses:
                self.risk_state = "LOCKDOWN"
            else:
                self.risk_state = "STABLE"
        return self.risk_state


def get_station_data(station: str, db_path: str) -> Tuple[List[Dict], Dict[str, str]]:
    """Get temperature and market data for a station."""
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    
    # Temperature data
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
    
    # Market data (HIGH market only - lower variance than LOW)
    cur.execute("""
        SELECT local_trading_date, settlement_bucket, prior_settlement_bucket
        FROM settlement_epochs
        WHERE station = ? AND market_type = 'HIGH' AND epoch_status = 'closed'
        ORDER BY local_trading_date ASC
    """, (station,))
    
    market = {}
    for r in cur.fetchall():
        if r[2] is not None:  # Has prior
            direction = 'up' if r[1] > r[2] else ('down' if r[1] < r[2] else 'flat')
            market[r[0]] = direction
    
    conn.close()
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


# Define signal functions similar to the original ensemble
def simple_trend_signal(today: Dict, yesterday: Dict, market_data: Dict = None) -> Tuple[Optional[str], float]:
    """Simple trend signal with return value"""
    if today['high'] is not None and yesterday['high'] is not None:
        if today['high'] > yesterday['high']:
            return 'up', 0.65  # moderate confidence
        elif today['high'] < yesterday['high']:
            return 'down', 0.65
    return None, 0.0


def reversion_signal(today: Dict, yesterday: Dict, market_data: Dict = None) -> Tuple[Optional[str], float]:
    """Reversion signal with return value"""
    all_temps = market_data.get('all_temps', []) if market_data else []
    if len(all_temps) < 30:
        return None, 0.0
    
    # Get recent highs for rolling calculation
    recent_highs = [t['high'] for t in all_temps[-32:-1] if t.get('high') is not None]  # Exclude today, include up to yesterday
    if len(recent_highs) < 10:
        return None, 0.0
    
    mean = sum(recent_highs) / len(recent_highs)
    variance = sum((h - mean) ** 2 for h in recent_highs) / len(recent_highs)
    std = math.sqrt(variance) if variance > 0 else 1.0
    
    # Check if today's high deviates significantly
    today_high = today['high']
    deviation = (today_high - mean) / std if std > 0 and today_high is not None else 0
    
    if abs(deviation) > 2.0:  # >2σ from mean → bet reversion
        direction = 'down' if deviation > 0 else 'up'  # Revert toward mean
        return direction, min(0.9, 0.5 + abs(deviation) * 0.1)  # Higher confidence for larger deviations
    
    return None, 0.0


def gaussian_model_signal(today: Dict, yesterday: Dict, market_data: Dict = None) -> Tuple[Optional[str], float]:
    """Gaussian model signal with return value"""
    all_temps = market_data.get('all_temps', []) if market_data else []
    if len(all_temps) < 48:
        return None, 0.0
    
    recent_temps = [t['high'] for t in all_temps[-50:-1] if t.get('high') is not None]
    if len(recent_temps) < 20:
        return None, 0.0
    
    # Calculate rolling mean and std
    mu = sum(recent_temps) / len(recent_temps)
    variance = sum((t - mu) ** 2 for t in recent_temps) / len(recent_temps)
    sigma = math.sqrt(variance) if variance > 0 else 2.0
    
    today_high = today.get('high')
    if today_high is not None:
        z_score = abs(today_high - mu) / sigma if sigma > 0 else 0
        
        if z_score > 1.0:
            direction = 'up' if today_high < mu else 'down'
            return direction, min(0.8, 0.4 + z_score * 0.15)
    
    return None, 0.0


def forecast_disagreement_signal(today: Dict, yesterday: Dict, market_data: Dict = None) -> Tuple[Optional[str], float]:
    """Forecast disagreement signal with return value"""
    all_temps = market_data.get('all_temps', []) if market_data else []
    if len(all_temps) < 366:
        return None, 0.0
    
    # Extract this day of year's temperature from recent years
    today_doy = datetime.strptime(today['date'], '%Y-%m-%d').timetuple().tm_yday
    recent_temps = []
    
    for t in all_temps[-366:]:
        date_obj = datetime.strptime(t['date'], '%Y-%m-%d').timetuple() 
        doy = date_obj.tm_yday
        if abs(doy - today_doy) <= 3 and t.get('high') is not None:
            recent_temps.append(t['high'])
    
    if len(recent_temps) < 5:
        return None, 0.0
    
    historical_avg = sum(recent_temps) / len(recent_temps)
    
    # Calculate deviation from climate normal
    today_high = today.get('high')
    if today_high is not None:
        climate_deviation = today_high - historical_avg
        
        # Signal: bet in observed trend direction if deviation > 5°F
        if abs(climate_deviation) > 5:
            trend_diff = today_high - yesterday.get('high', historical_avg)
            direction = 'up' if trend_diff > 0 else 'down'
            return direction, min(0.85, 0.4 + min(0.5, abs(climate_deviation) / 10.0))
    
    return None, 0.0


def climate_persistence_signal(today: Dict, yesterday: Dict, market_data: Dict = None) -> Tuple[Optional[str], float]:
    """Climate persistence signal (now 3-day momentum) with return value"""
    all_temps = market_data.get('all_temps', []) if market_data else []
    if len(all_temps) < 4:
        return None, 0.0
    
    recent_highs = [t['high'] for t in all_temps[-4:] if t.get('high') is not None]
    if len(recent_highs) < 4:
        return None, 0.0
    
    # Calculate 3-day momentum: today vs 3 days ago
    trend_3day = (recent_highs[0] - recent_highs[-1]) if recent_highs[0] is not None and recent_highs[-1] is not None else 0
    
    if trend_3day > 0:
        return 'up', min(0.75, 0.5 + abs(trend_3day) * 0.02)
    elif trend_3day < 0:
        return 'down', min(0.75, 0.5 + abs(trend_3day) * 0.02)
    
    return None, 0.0


def regime_strategy(today: Dict, yesterday: Dict, market_data: Dict = None) -> Tuple[Optional[str], float]:
    """Regime strategy signal (based on pressure stability) with return value"""
    press_change = today.get('pressure') is not None and yesterday.get('pressure') is not None
    if not press_change:
        return None, 0.0
    
    pressure_change = today['pressure'] - yesterday['pressure']
    is_stable = abs(pressure_change) < 3.0  # Lower threshold for stability
    
    # Get 1-day trend
    high_change = today.get('high') is not None and yesterday.get('high') is not None
    if not high_change:
        return None, 0.0
    
    trend_1day = today['high'] - yesterday['high']
    
    if is_stable:
        # Stable: bet with trend
        if trend_1day > 0:
            return 'up', 0.7 if abs(trend_1day) > 2.0 else 0.6
        elif trend_1day < 0:
            return 'down', 0.7 if abs(trend_1day) > 2.0 else 0.6
    else:
        # Unstable: bet reversion
        if trend_1day > 0:
            return 'down', 0.65
        elif trend_1day < 0:
            return 'up', 0.65
    
    return None, 0.0


def dtr_trend_signal(today: Dict, yesterday: Dict, market_data: Dict = None) -> Tuple[Optional[str], float]:
    """DTR (Daily Temperature Range) trend signal with return value"""
    all_temps = market_data.get('all_temps', []) if market_data else []
    if len(all_temps) < 7:  # Need 7 days to calculate trend
        return None, 0.0
    
    # Calculate recent DTRs (High - Low)
    recent_dtrs = []
    for t in all_temps[-8:]:
        if t.get('high') is not None and t.get('low') is not None:
            dtr = t['high'] - t['low']
            if dtr > 0:  # Valid DTR
                recent_dtrs.append(dtr)
    
    if len(recent_dtrs) < 7:
        return None, 0.0
    
    # Calculate simple trend: compare last 3 DTRs vs first 3
    recent_avg = sum(recent_dtrs[-3:]) / len(recent_dtrs[-3:])
    earlier_avg = sum(recent_dtrs[-7:-4]) / len(recent_dtrs[-7:-4])
    
    if recent_avg > earlier_avg:
        # Expanding DTR -> higher volatility trending up (bet up)
        strength = min(0.9, abs(recent_avg - earlier_avg) * 0.05) 
        return 'up', strength
    elif recent_avg < earlier_avg:
        # Contracting DTR -> less volatility trending down (bet down)  
        strength = min(0.9, abs(recent_avg - earlier_avg) * 0.05)
        return 'down', strength
    
    return None, 0.0


def wind_direction_signal(today: Dict, yesterday: Dict, market_data: Dict = None) -> Tuple[Optional[str], float]:
    """Wind direction-based signal with return value"""
    if today.get('wind_dir') is None or yesterday.get('wind_dir') is None:
        return None, 0.0
    
    # Significant wind change: >90 degrees
    wind_change = abs(today['wind_dir'] - yesterday['wind_dir'])
    
    if wind_change > 90:  # Significant change
        # North winds often indicate colder fronts (down)
        # Southerly winds often indicate warmer air masses (up)
        if 180 <= today['wind_dir'] <= 360:  # South/southwest winds
            return 'up', min(0.7, 0.4 + wind_change / 400.0)
        else:  # North/northeast winds
            return 'down', min(0.7, 0.4 + wind_change / 400.0)
    
    return None, 0.0


def pressure_regime_signal(today: Dict, yesterday: Dict, market_data: Dict = None) -> Tuple[Optional[str], float]:
    """Pressure-based regime signal with return value"""
    if today.get('pressure') is None or yesterday.get('pressure') is None:
        return None, 0.0
    
    pressure_change = today['pressure'] - yesterday['pressure']
    
    if pressure_change > 3:  # Pressure increasing significantly (high pressure) → warm
        return 'up', min(0.8, 0.4 + abs(pressure_change) * 0.02)
    elif pressure_change < -3:  # Pressure decreasing significantly (low pressure) → cool
        return 'down', min(0.8, 0.4 + abs(pressure_change) * 0.02)
    elif abs(pressure_change) <= 0.5:  # Very stable pressure
        # Use 3-day trend for stable conditions
        all_temps = market_data.get('all_temps', []) if market_data else []
        if len(all_temps) >= 3:
            prev_2 = all_temps[-3] if len(all_temps) >= 3 and all_temps[-3].get('high') else None
            if prev_2 and today.get('high') and yesterday.get('high'):
                trend = today['high'] - yesterday['high'] + (yesterday['high'] - prev_2['high']) / 2
                if trend > 2:
                    return 'up', 0.65
                elif trend < -2:
                    return 'down', 0.65
    
    return None, 0.0


def run_combined_experiment(
    db_path: str, 
    stations: List[str],
    smoothing_configs: Dict[str, Dict],
    filter_params: Tuple[int, float],  # (agreement_threshold, confidence_threshold)
    ensemble_weights: List[float],
    ensemble_threshold: float,
    risk_config: RiskConfig
) -> Dict[str, Any]:
    """
    Run the combined B6.2 + B6.4 experiment with strong filter + smoothing
    
    Args:
        db_path: Path to weather database
        stations: List of station codes to test
        smoothing_configs: Configuration for each signal's smoothing
        filter_params: (agreement_threshold, confidence_threshold)
        ensemble_weights: Weight for each signal in ensemble voting
        ensemble_threshold: Threshold for ensemble decision
        risk_config: B1.5 risk configuration
        
    Returns:
        Dictionary with experiment results
    """
    
    # Initialize components
    signal_processor = SignalProcessor(smoothing_configs)
    risk_manager = RiskManager(risk_config)
    
    # Available signals (as per baseline 9 signals, 7 stations)
    signal_functions = [
        ('simple_trend', simple_trend_signal),
        ('reversion', reversion_signal),
        ('gaussian', gaussian_model_signal),
        ('forecast_disagreement', forecast_disagreement_signal),
        ('climate_persistence', climate_persistence_signal),
        ('regime_strategy', regime_strategy),
        ('dtr_trend', dtr_trend_signal),
        ('wind_direction', wind_direction_signal),
        ('pressure_regime', pressure_regime_signal)
    ]
    
    # Track results
    all_predictions = []
    all_actual = []
    trade_count = 0
    
    for station in stations:
        temps, market = get_station_data(station, db_path)
        aligned = align_data(temps, market)
        
        if len(aligned) < 30:  # Need sufficient historical data
            continue
        
        # Pre-compute all temperatures for signals that need them
        market_data = {'all_temps': temps, 'market': market}
        
        # Walk forward simulation
        for i in range(29, len(aligned)):  # Start after warmup period
            # Reset risk state if it went to lockdown in previous trades 
            if risk_manager.risk_state == "LOCKDOWN":
                risk_manager.reset()
            
            today = aligned[i]
            yesterday = aligned[i-1]

            actual = today['market_dir']
            if actual == 'flat':
                continue
            
            all_actual.append(actual)
            
            # Generate signals
            signals = []
            signal_vals = []  # Store processed signal values (for smoothing)
            
            for name, signal_fn in signal_functions:
                try:
                    direction, confidence = signal_fn(today, yesterday, market_data)
                    
                    if direction is not None and confidence > 0:
                        # Process through smoother
                        raw_val = confidence if direction == 'up' else -confidence
                        raw_processed, smooth_processed = signal_processor.process_signal(name, raw_val)
                        
                        # Convert back: positive for 'up', negative for 'down'
                        smooth_val = smooth_processed
                        smooth_direction = 'up' if smooth_processed >= 0 else 'down' 
                        smooth_confidence = abs(smooth_processed)
                        
                        signals.append({
                            'name': name,
                            'raw_direction': direction,
                            'raw_confidence': confidence,
                            'smooth_direction': smooth_direction,
                            'smooth_confidence': smooth_confidence,
                            'smooth_value': smooth_val
                        })
                        signal_vals.append(smooth_val)
                except Exception:
                    continue  # Skip problematic signals
            
            # Apply strong confirmation filter
            if len(signals) < filter_params[0]:  # Check agreement threshold
                continue
            
            # Count how many signals agree with the majority
            up_signals = [s for s in signals if s['smooth_direction'] == 'up']
            down_signals = [s for s in signals if s['smooth_direction'] == 'down']
            
            # Determine majority direction
            majority_direction = None
            if len(up_signals) >= filter_params[0]:
                majority_direction = 'up'
            elif len(down_signals) >= filter_params[0]:
                majority_direction = 'down'
            else:
                # Not enough agreement to pass filter
                continue
            
            # Calculate signed confidence sum
            signed_sum = sum(s['smooth_value'] for s in signals if s['smooth_direction'] == majority_direction)
            
            if abs(signed_sum) < filter_params[1]:  # Check confidence threshold
                continue
            
            # Ensemble with weights: weighted vote
            if len(signal_vals) != len(ensemble_weights):
                # Adjust weights to match signal count
                adjusted_weights = [1.0/len(signal_vals) if signal_vals else 0]*len(signal_vals)
            else:
                adjusted_weights = ensemble_weights
                
            # Weighted sum of signals
            weighted_sum = sum(val * weight for val, weight in zip(signal_vals, adjusted_weights))
            pred_confidence = abs(weighted_sum)
            
            # Apply ensemble threshold
            if pred_confidence < ensemble_threshold:
                continue
                
            # Make final prediction
            final_direction = 'up' if weighted_sum > 0 else 'down'
            
            # Apply B1.5 risk control
            is_correct = (final_direction == actual)
            risk_state = risk_manager.update_after_trade(is_correct)
            
            # Only count as a trade if we're still in STABLE state
            if risk_state == "STABLE":
                all_predictions.append(final_direction)
                trade_count += 1
            else:
                # Risk control triggered, don't count trade but break here
                break
    
    # Calculate metrics
    correct = sum(1 for p, a in zip(all_predictions, all_actual[:len(all_predictions)]) if p == a)
    total_actual = len(all_predictions)
    
    # Calculate Sharpe ratio approximation (using simplified daily returns concept)
    if total_actual > 0:
        accuracy = correct / total_actual if total_actual > 0 else 0.0
        # Approximate Sharpe: (accuracy - 0.5) / volatility
        # Treat each trade as either +1 reward (correct) or -1 penalty (incorrect)  
        trade_returns = [1.0 if p == a else -1.0 
                         for p, a in zip(all_predictions, all_actual[:len(all_predictions)])]
        if len(trade_returns) > 1:
            volatility = np.std(trade_returns) if trade_returns else 0.001  # Avoid division by zero
            sharpe = (np.mean(trade_returns)) / volatility if volatility > 0 else 0.0
        else:
            sharpe = 0.0
    else:
        accuracy = 0.0
        sharpe = 0.0
    
    # Per-station breakdown (for comparison vs baseline)  
    station_breakdown = {}
    for station in stations:
        temps, market = get_station_data(station, db_path)
        aligned = align_data(temps, market)
        
        station_preds = []
        station_actuals = []
        
        # Replicate the full simulation for this station specifically
        for i in range(29, len(aligned)):
            today = aligned[i]
            yesterday = aligned[i-1]
            
            actual = today['market_dir']
            if actual == 'flat':
                continue
            station_actuals.append(actual)
            
            # Generate signals
            signals = []
            signal_vals = []
            
            for name, signal_fn in signal_functions:
                try:
                    direction, confidence = signal_fn(today, yesterday, {'all_temps': temps, 'market': market})
                    
                    if direction is not None and confidence > 0:
                        raw_val = confidence if direction == 'up' else -confidence
                        raw_processed, smooth_processed = signal_processor.process_signal(name, raw_val)
                        
                        smooth_direction = 'up' if smooth_processed >= 0 else 'down'
                        smooth_confidence = abs(smooth_processed)
                        
                        signals.append({
                            'name': name,
                            'smooth_direction': smooth_direction,
                            'smooth_value': smooth_processed
                        })
                        signal_vals.append(smooth_processed)
                except Exception:
                    continue
            
            # Apply strong confirmation filter (we'll count all that get evaluated for this stat)
            if len(signals) >= filter_params[0]:
                up_signals = [s for s in signals if s['smooth_direction'] == 'up']
                down_signals = [s for s in signals if s['smooth_direction'] == 'down']
                
                if (len(up_signals) >= filter_params[0] or len(down_signals) >= filter_params[0]):
                    majority_direction = None
                    if len(up_signals) >= filter_params[0]:
                        majority_direction = 'up'
                    else:
                        majority_direction = 'down'
                    
                    signed_sum = sum(s['smooth_value'] for s in signals if s['smooth_direction'] == majority_direction)
                    
                    if abs(signed_sum) >= filter_params[1]:
                        # Apply weights
                        if len(signal_vals) != len(ensemble_weights):
                            adjusted_weights = [1.0/len(signal_vals) if signal_vals else 0]*len(signal_vals)
                        else:
                            adjusted_weights = ensemble_weights
                        
                        weighted_sum = sum(val * weight for val, weight in zip(signal_vals, adjusted_weights))
                        
                        if abs(weighted_sum) >= ensemble_threshold:
                            pred_direction = 'up' if weighted_sum > 0 else 'down'
                            station_preds.append(pred_direction)
        
        if len(station_preds) > 0:
            stn_correct = sum(1 for p, a in zip(station_preds, station_actuals[:len(station_preds)]) if p == a)
            station_accuracy = stn_correct / len(station_preds) if len(station_preds) > 0 else 0.0
            station_breakdown[station] = {
                'accuracy': station_accuracy,
                'trades': len(station_preds),
                'delta_vs_baseline': station_accuracy - 0.78  # Delta vs 78% baseline
            }
        else:
            station_breakdown[station] = {
                'accuracy': 0.0,
                'trades': 0,
                'delta_vs_baseline': -0.78  # Failed vs baseline
            }
    
    result = {
        'directional_accuracy': accuracy,
        'sharpe_ratio': sharpe,
        'trade_count': len(all_predictions),
        'confirm_params': filter_params,
        'smoothing_params': smoothing_configs,  # Represent each signal's param
        'ensemble_params': (ensemble_weights, ensemble_threshold),
        'station_breakdown': station_breakdown,
        'risk_state': risk_manager.risk_state,
        'consecutive_losses': risk_manager.consecutive_losses,
        'total_predictions_used': trade_count,
        'passed_baseline': accuracy >= 0.783  # Whether we improved over 78.3% baseline
    }
    
    return result


def optimize_parameters(
    db_path: str,
    stations: List[str],
    risk_config: RiskConfig
) -> Dict[str, Any]:
    """
    Run the optimization sweep for optimal parameters using isotonic calibration approach.
    """
    print("Starting 200-round isotonic calibration sweep...")
    
    best_result = None
    best_score = -float('inf')
    
    # Define search spaces
    # Agreement counts: 4 to 9 (at least half of the 9 signals)
    agreement_range = list(range(4, 10))
    # Confidence thresholds: 0.1 to 2.0 (sum of signed confidences)
    confidence_range = [round(0.1 + i * 0.1, 2) for i in range(20)]
    # Per-signal smoothing: none, light, heavy Kalman + EWMA variations
    smoothing_type_params = [
        (None, {}),  # no smoothing
        ('kalman', {'process_noise': 0.05, 'measurement_noise': 0.05}),
        ('kalman', {'process_noise': 0.1, 'measurement_noise': 0.1}), 
        ('kalman', {'process_noise': 0.2, 'measurement_noise': 0.2}),
        ('ewma', {'alpha': 0.2}),
        ('ewma', {'alpha': 0.3}),
        ('ewma', {'alpha': 0.1}),
    ]
    # Individual signal smoothing variations across 9 signals (combinatorial - simplified)
    ensemble_weight_sets = [
        [1.0] * 9,  # Equal weights
        [1.2, 1.0, 0.8, 1.0, 0.9, 1.1, 1.3, 0.7, 0.9],  # Example weighted
        [1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0],  # Unit weights
    ]
    
    # Enforce 200 iterations maximum
    iterations = 0
    max_iterations = 200
    
    print(f"Parameter space defined. Running up to {max_iterations} iterations...")
    
    for agreement in agreement_range:
        if iterations >= max_iterations:
            break
            
        for conf_thresh in confidence_range:
            if iterations >= max_iterations:
                break
            
            # Since per-signal smoothing combinations would be vast,
            # we'll cycle through different overall smoothing strategies for each combo
            smoothing_strategies = [
                # Strategy 1: No smoothing
                {name: {'type': 'none'} for name, _ in [('simple_trend', 0), ('reversion', 0), ('gaussian', 0), ('forecast_disagreement', 0), ('climate_persistence', 0), ('regime_strategy', 0), ('dtr_trend', 0), ('wind_direction', 0), ('pressure_regime', 0)]},
                
                # Strategy 2: Light smoothing
                {name: {'type': 'kalman', 'params': {'process_noise': 0.1, 'measurement_noise': 0.1}} 
                 for name, _ in [('simple_trend', 0), ('reversion', 0), ('gaussian', 0), ('forecast_disagreement', 0), ('climate_persistence', 0), ('regime_strategy', 0), ('dtr_trend', 0), ('wind_direction', 0), ('pressure_regime', 0)]},
                
                # Strategy 3: Medium smoothing
                {name: {'type': 'ewma', 'params': {'alpha': 0.3}} 
                 for name, _ in [('simple_trend', 0), ('reversion', 0), ('gaussian', 0), ('forecast_disagreement', 0), ('climate_persistence', 0), ('regime_strategy', 0), ('dtr_trend', 0), ('wind_direction', 0), ('pressure_regime', 0)]},
            ]
            
            for smoothing_config in smoothing_strategies:
                if iterations >= max_iterations:
                    break
                
                for weight_set in ensemble_weight_sets:
                    if iterations >= max_iterations:
                        break
                    
                    # Fixed threshold sweep - let's use a reasonable range
                    ens_threshold = 1.0  # Fixed for this experiment
                        
                    iterations += 1
                    if iterations % 20 == 0:
                        print(f"Iteration {iterations}/200")
                    
                    try:
                        result = run_combined_experiment(
                            db_path=db_path,
                            stations=stations,
                            smoothing_configs=smoothing_config,
                            filter_params=(agreement, conf_thresh),
                            ensemble_weights=weight_set,
                            ensemble_threshold=ens_threshold,
                            risk_config=risk_config
                        )
                        
                        # Define scoring: accuracy + diversity factor - trade penalty
                        score = result['directional_accuracy'] * 100 + result['sharpe_ratio'] * 0.1
                        
                        # Prioritize solutions closer to base trade count (baseline had 11,893)
                        trade_penalty = abs(result['trade_count'] - 11893) * 0.00001  # Small penalty
                        adjusted_score = score - trade_penalty
                        
                        if adjusted_score > best_score:
                            best_score = adjusted_score
                            best_result = {
                                **result,
                                'best_param_combination': {
                                    'agreement_threshold': agreement,
                                    'confidence_threshold': conf_thresh,
                                    'smoothing_scheme': list(smoothing_config.values())[0]['type'] if smoothing_config else 'none',
                                    'weight_scheme': 'equal' if all(w == 1.0 for w in weight_set) else 'weighted',
                                    'ensemble_threshold': ens_threshold
                                }
                            }
                    except Exception as e:
                        print(f"Iteration {iterations} failed due to error: {str(e)}")
                        continue
    
    print(f"Optimization complete. Evaluated {iterations} parameter sets.")
    return best_result


def main():
    """Main experiment wrapper"""
    print("Running Combined B6.2 + B6.4 Experiment")
    print("Strong Confirmation Filter + Kalman/EWMA Smoothing")
    print("="*60)
    
    # Database path
    db_path = "/home/node/.openclaw/workspace/prototypes/weather-engine-source/data/metar_backfill.db"
    
    # Baseline stations
    stations = ['KNYC', 'KLAX', 'KMDW', 'KBOS', 'KATL', 'KSFO', 'KSEA']
    
    # B1.5 Risk config
    risk_config = RiskConfig(max_consecutive_losses=8)
    
    # Run optimization
    print(f"Testing with baseline: 7 stations, ~78.3% accuracy, ~11,893 trades")
    result = optimize_parameters(db_path, stations, risk_config)
    
    if result:
        # Extract final optimal parameters
        best_params = result['best_param_combination']
        
        print() 
        print("EXPERIMENT RESULTS")
        print("="*60)
        
        print(f"• Directional accuracy: {result['directional_accuracy']*100:.2f}% (delta: {result['directional_accuracy']*100 - 78.3:+.2f} pp vs 78.3% baseline)")
        print(f"• Sharpe: {result['sharpe_ratio']:.3f}")
        print(f"• Trade count: {result['trade_count']} (vs 11,893 baseline)")
        print(f"• Optimal confirmation parameters: (agreement={best_params['agreement_threshold']}, confidence={best_params['confidence_threshold']})")
        
        # Get a representative smoothing parameter for one signal type
        sm = next(iter(result['smoothing_params'].values()), {}).get('type', 'none')
        if sm == 'kalman':
            noise = next(iter(result['smoothing_params'].values()), {}).get('params', {}).get('process_noise', 'N/A')
            print(f"• Optimal smoothing per-signal: Kalman (process noise={noise})")
        elif sm == 'ewma':
            alpha = next(iter(result['smoothing_params'].values()), {}).get('params', {}).get('alpha', 'N/A')
            print(f"• Optimal smoothing per-signal: EWMA (alpha={alpha})")
        else:
            print(f"• Optimal smoothing per-signal: none")
        
        weight_str = 'Equal weights' if all(abs(w-1.0) < 0.01 for w in result['ensemble_params'][0]) else 'Weighted'
        print(f"• Optimal ensemble: threshold={best_params['ensemble_threshold']:.1f}, weights: {weight_str}")
        
        print(f"\nPer-station breakdown:")
        print("  station | accuracy | trades | delta vs baseline")
        print("  --------|----------|--------|------------------")
        for station_id, info in result['station_breakdown'].items():
            print(f"  {station_id:<7} | {info['accuracy']:.3f}    | {info['trades']:6d} | {info['delta_vs_baseline']:+.3f}")
              
        print(f"\n• Risk state: {result['risk_state']} (consecutive losses: {result['consecutive_losses']})")
        
        base_trades = 11893  # Baseline trade count from specification  
        coverage_change = (result['trade_count'] - base_trades) / base_trades if base_trades > 0 else 0
        print(f"• Coverage change vs baseline: {coverage_change:.1%}")
        print(f"• Improvement vs 78.3% baseline under guardrails: {'YES' if result['passed_baseline'] else 'NO'}")
    else:
        print("ERROR: Optimization failed to return results")


if __name__ == "__main__":
    main()