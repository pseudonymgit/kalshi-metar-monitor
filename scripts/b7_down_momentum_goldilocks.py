#!/usr/bin/env python3
"""
B7 DOWN-Momentum / Goldilocks Symmetry Implementation

This script implements the DOWN market signal symmetry (mirror the UP signals
for Goldilocks/reversion) using the B7 baseline and per-station skill gating.

Target: make Goldilocks lane produce clean DOWN signals with proper graded alerts.

Usage:
    python scripts/b7_down_momentum_goldilocks.py [--stations <stations>] [--output-dir <dir>]

Output:
    - b7_down_momentum_goldilocks_<timestamp>.json
    - b7_down_momentum_goldilocks_summary_<timestamp>.md
"""

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from typing import Dict, List, Tuple, Optional, Any
from collections import defaultdict
import statistics

# Add parent directory to path for imports
SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, os.path.join(REPO_ROOT, "core"))
sys.path.insert(0, os.path.join(REPO_ROOT, "core/signals"))

from signals import GoldilocksSignal, SignalRegistry
from signal_fusion import SignalFusionEngine
from signal_processors.skill_gating import get_skill_gating_params
from calibration_pipeline import CalibrationPipeline


# ==============================================================================
# CONFIGURATION
# ==============================================================================

# B7 baseline parameters (from staged calibration results)
B7_BASELINE = {
    'confirmation_filter': {
        'min_confidence': 0.7,
        'max_confidence': 0.85
    },
    'risk_controls': {
        'max_consecutive_losses': 8,
        'max_drawdown': 0.15
    },
    'signal_weights': {
        'goldilocks': 1.0,  # Goldilocks gets full weight in DOWN lane
    }
}

# 20 stations for full ensemble
ALL_STATIONS = [
    "KATL", "KAUS", "KBOS", "KDCA", "KDEN", "KDFW", "KHOU",
    "KLAS", "KLAX", "KMDW", "KMIA", "KMSP", "KMSY", "KNYC",
    "KOKC", "KPHL", "KPHX", "KSAT", "KSEA", "KSFO"
]

# Signal configuration for DOWN lane
DOWN_LANE_SIGNALS = {
    'goldilocks': {
        'name': 'Goldilocks DOWN (spike up → reversion down)',
        'direction': 'down',
        'base_confidence': 0.40
    }
}

# Output directory
OUTPUT_DIR = os.path.join(REPO_ROOT, "results")
os.makedirs(OUTPUT_DIR, exist_ok=True)


# ==============================================================================
# DATABASE HELPER FUNCTIONS
# ==============================================================================

def get_station_data(station: str, db_path: str) -> List[Dict[str, Any]]:
    """Get temperature data for a station from the METAR database."""
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT date_utc, MAX(temp_f) as high, MIN(temp_f) as low,
                   AVG(dewpoint_f) as dewpoint, AVG(temp_f) as temp,
                   AVG(wind_direction_deg) as wind_dir, AVG(wind_speed_kt) as wind_speed,
                   AVG(pressure_mb) as pressure
            FROM metar_observations
            WHERE station = ? AND temp_f IS NOT NULL AND pressure_mb IS NOT NULL
            GROUP BY date_utc
            ORDER BY date_utc ASC
        """, (station,))
        
        days = []
        for r in cur.fetchall():
            if any(v is None for v in r[1:]):
                continue
            days.append({
                'date': r[0], 'high': r[1], 'low': r[2],
                'dewpoint': r[3], 'temp': r[4],
                'wind_dir': r[5], 'wind_speed': r[6], 'pressure': r[7]
            })
        return days
    finally:
        conn.close()


def get_market_data(station: str, db_path: str) -> Dict[str, str]:
    """Get settlement epoch market data for a station."""
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
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
        return market
    finally:
        conn.close()


def align_data(days: List[Dict], market: Dict[str, str]) -> List[Dict]:
    """Align temperature data with market direction data."""
    aligned = []
    for day in days:
        if day['date'] in market:
            aligned.append({
                **day, 'market_dir': market[day['date']]
            })
    return aligned


# ==============================================================================
# DOWN-FOCUSED SIGNAL EVALUATORS
# ==============================================================================

def goldilocks_down_signal(idx: int, days: List[Dict]) -> Tuple[Optional[str], float]:
    """
    Goldilocks DOWN signal: looks for spike up → reversion down patterns.
    
    This is the DOWN-market counterpart to the standard Goldilocks signal.
    It specifically targets upward spikes that reverse downward.
    
    Returns:
        (direction, confidence) or (None, 0.0) if no signal
    """
    if len(days) < 2:
        return None, 0.0
    
    if idx < 1:
        return None, 0.0
    
    today = days[idx]
    yesterday = days[idx - 1]
    
    # Calculate temperature change
    temp_change = today['high'] - yesterday['high']
    
    # Spike threshold: > 2°F upward change (spike up)
    spike_threshold = 2.0
    is_spike_up = temp_change >= spike_threshold
    
    if not is_spike_up:
        return None, 0.0
    
    # We're looking for reversion DOWN after spike UP
    # Current day should show downward momentum from the spike
    
    # Compute confidence using asymmetric scoring
    # Per Gray Room Round 4-1.5: Up reversion (spike up → drop) base 0.40
    base = 0.40
    daily_high_margin = max(0.0, temp_change / 5.0)
    confidence = base + daily_high_margin * 0.15
    confidence = min(1.0, confidence)
    
    return 'down', confidence


def apply_confirmation_filter(directions: List[str], confidences: List[float],
                               threshold: int = 2) -> Tuple[Optional[str], float]:
    """
    Apply confirmation filter: require minimum number of signals agreeing.
    
    Args:
        directions: List of signal directions ('up' or 'down')
        confidences: List of corresponding confidences
        threshold: Minimum number of signals that must agree
    
    Returns:
        (direction, confidence) or (None, 0.0) if filter fails
    """
    if len(directions) < threshold:
        return None, 0.0
    
    # Count agreement
    down_count = sum(1 for d in directions if d == 'down')
    up_count = sum(1 for d in directions if d == 'up')
    
    # Require at least 2 signals agreeing on DOWN
    if down_count >= threshold:
        # Weighted average confidence for DOWN signals
        down_confs = [c for d, c in zip(directions, confidences) if d == 'down']
        avg_confidence = sum(down_confs) / len(down_confs)
        return 'down', avg_confidence
    elif up_count >= threshold:
        up_confs = [c for d, c in zip(directions, confidences) if d == 'up']
        avg_confidence = sum(up_confs) / len(up_confs)
        return 'up', avg_confidence
    
    return None, 0.0


# ==============================================================================
# DOWN LANE TRADER
# ==============================================================================

class DownLaneTrader:
    """Trader专注于DOWN lane, 使用 Goldilocks asymmetry."""
    
    def __init__(self, db_path: str, stations: List[str] = None,
                 baseline: Dict = None, skill_gating_active: bool = True):
        self.db_path = db_path
        self.stations = stations or ALL_STATIONS
        self.baseline = baseline or B7_BASELINE
        self.skill_gating_active = skill_gating_active
        
        # Initialize signal registry with Goldilocks
        self.signal_registry = SignalRegistry(db_path)
        
        # Get skill gating data
        self.skill_data = get_skill_gating_params()
        
        # Initialize calibration pipeline
        self.calibration_pipeline = CalibrationPipeline(
            signal_names=['goldilocks'],
            city_codes=self.stations
        )
        
        # Initialize fusion engine
        self.fusion_engine = SignalFusionEngine(
            signal_names=['goldilocks'],
            city_codes=self.stations
        )
    
    def get_station_skill(self, station: str) -> float:
        """Get station's Brier Skill Score for skill gating."""
        station_info = self.skill_data.get('station_results', {}).get(station, {})
        return station_info.get('brier_skill_score', 0.0)
    
    def run_station_backtest(self, station: str) -> Dict[str, Any]:
        """Run backtest for a single station in the DOWN lane."""
        days = get_station_data(station, self.db_path)
        market = get_market_data(station, self.db_path)
        aligned = align_data(days, market)
        
        if len(aligned) < 30:
            return {'station': station, 'error': 'Insufficient data'}
        
        # Check skill gating
        if self.skill_gating_active:
            station_skill = self.get_station_skill(station)
            if station_skill <= 0.0:  # Threshold for skilled stations
                return {'station': station, 'error': 'Station not skilled'}
        
        results = {
            'station': station,
            'total_trades': 0,
            'correct_predictions': 0,
            'incorrect_predictions': 0,
            'consecutive_losses': 0,
            'max_consecutive_losses': 0,
            'pnl': 0.0,
            'returns': [],
            'predictions': []
        }
        
        min_confidence = self.baseline['confirmation_filter']['min_confidence']
        max_confidence = self.baseline['confirmation_filter']['max_confidence']
        
        for i in range(1, len(aligned)):
            today = aligned[i]
            yesterday = aligned[i - 1]
            
            actual = today['market_dir']
            if actual == 'flat':
                continue
            
            # Get Goldilocks DOWN signal
            direction, confidence = goldilocks_down_signal(i, aligned[:i+1])
            
            if direction is None:
                continue
            
            # Apply confirmation filter
            confirmed_direction, confirmed_confidence = apply_confirmation_filter(
                [direction], [confidence],
                threshold=1  # Single signal for DOWN lane
            )
            
            if confirmed_direction is None:
                continue
            
            # Apply confidence threshold
            if not (min_confidence <= confirmed_confidence <= max_confidence):
                continue
            
            # Execute trade
            is_correct = confirmed_direction == actual
            results['total_trades'] += 1
            results['returns'].append(1.0 if is_correct else -1.0)
            results['pnl'] += 1.0 if is_correct else -1.0
            
            if is_correct:
                results['correct_predictions'] += 1
                results['consecutive_losses'] = 0
            else:
                results['incorrect_predictions'] += 1
                results['consecutive_losses'] += 1
                results['max_consecutive_losses'] = max(
                    results['max_consecutive_losses'],
                    results['consecutive_losses']
                )
            
            # Check risk controls
            if results['consecutive_losses'] >= self.baseline['risk_controls']['max_consecutive_losses']:
                break  # Stop trading if hit consecutive loss limit
            
            results['predictions'].append({
                'date': today['date'],
                'predicted': confirmed_direction,
                'actual': actual,
                'confidence': confirmed_confidence,
                'is_correct': is_correct
            })
        
        # Calculate metrics
        if results['total_trades'] > 0:
            results['accuracy'] = results['correct_predictions'] / results['total_trades']
            results['directional_accuracy'] = results['accuracy']
            results['trade_count'] = results['total_trades']
            
            # Calculate Sharpe ratio
            if len(results['returns']) > 1:
                mean_return = statistics.mean(results['returns'])
                std_return = statistics.stdev(results['returns']) if len(results['returns']) > 1 else 0.01
                results['sharpe_ratio'] = mean_return / std_return if std_return > 0 else 0.0
        else:
            results['accuracy'] = 0.0
            results['sharpe_ratio'] = 0.0
            results['directional_accuracy'] = 0.0
            results['trade_count'] = 0
        
        return results
    
    def run_full_backtest(self) -> Dict[str, Any]:
        """Run full backtest across all stations in the DOWN lane."""
        results = {}
        for station in self.stations:
            print(f"  Processing {station}...")
            station_results = self.run_station_backtest(station)
            results[station] = station_results
        
        return results
    
    def aggregate_results(self, results: Dict[str, Any]) -> Dict[str, Any]:
        """Aggregate results across all stations."""
        accuracy_values = []
        sharpe_values = []
        trade_counts = []
        max_losses_values = []
        
        for station, station_results in results.items():
            if 'error' not in station_results:
                if station_results.get('total_trades', 0) > 0:
                    accuracy_values.append(station_results['accuracy'])
                    sharpe_values.append(station_results['sharpe_ratio'])
                    trade_counts.append(station_results['trade_count'])
                    max_losses_values.append(station_results['max_consecutive_losses'])
        
        aggregated = {
            'directional_accuracy': {
                'mean': statistics.mean(accuracy_values) if accuracy_values else 0,
                'median': statistics.median(accuracy_values) if accuracy_values else 0,
                'count': len(accuracy_values)
            } if accuracy_values else {},
            'sharpe_ratio': {
                'mean': statistics.mean(sharpe_values) if sharpe_values else 0,
                'median': statistics.median(sharpe_values) if sharpe_values else 0,
            } if sharpe_values else {},
            'trade_count': {
                'mean': statistics.mean(trade_counts) if trade_counts else 0,
                'median': statistics.median(trade_counts) if trade_counts else 0,
            } if trade_counts else {},
            'max_consecutive_losses': {
                'mean': statistics.mean(max_losses_values) if max_losses_values else 0,
                'median': statistics.median(max_losses_values) if max_losses_values else 0,
            } if max_losses_values else {},
            'station_breakout': results,
            'station_count': len(results),
            'successful_stations': len(accuracy_values)
        }
        
        return aggregated


# ==============================================================================
# MAIN EXECUTION
# ==============================================================================

def main():
    """Main entry point for DOWN-Momentum Goldilocks backtest."""
    print("=" * 70)
    print("B7 DOWN-Momentum / Goldilocks Symmetry Implementation")
    print("=" * 70)
    
    # Parse arguments
    stations = ALL_STATIONS
    output_dir = OUTPUT_DIR
    i = 1
    while i < len(sys.argv):
        if sys.argv[i] == '--stations' and i + 1 < len(sys.argv):
            stations = sys.argv[i + 1].split(',')
            i += 2
        elif sys.argv[i] == '--output-dir' and i + 1 < len(sys.argv):
            output_dir = sys.argv[i + 1]
            i += 2
        else:
            i += 1
    
    # Find METAR database
    db_path = os.path.join(REPO_ROOT, "data", "metar_backfill.db")
    if not os.path.exists(db_path):
        # Try alternate locations
        alt_paths = [
            os.path.join(REPO_ROOT, "data", "metar.db"),
            os.path.join(REPO_ROOT, "metar_backfill.db"),
            "/home/node/.openclaw/workspace/data/metar_backfill.db"
        ]
        for alt in alt_paths:
            if os.path.exists(alt):
                db_path = alt
                break
        else:
            print(f"ERROR: Could not find METAR database")
            print(f"  Searched: {db_path}")
            print(f"  Alternatives: {alt_paths}")
            sys.exit(1)
    
    print(f"METAR Database: {db_path}")
    print(f"Stations: {len(stations)} - {', '.join(stations)}")
    print(f"Output Directory: {output_dir}")
    print()
    
    # Create trader instance
    trader = DownLaneTrader(
        db_path=db_path,
        stations=stations,
        baseline=B7_BASELINE,
        skill_gating_active=True
    )
    
    # Run backtest
    print("Running DOWN lane backtest...")
    results = trader.run_full_backtest()
    
    # Aggregate results
    print("Aggregating results...")
    aggregated = trader.aggregate_results(results)
    
    # Generate timestamp
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    
    # Write results JSON
    output_path = os.path.join(output_dir, f"b7_down_momentum_goldilocks_{timestamp}.json")
    with open(output_path, 'w') as f:
        json.dump(aggregated, f, indent=2, default=str)
    print(f"Results written: {output_path}")
    
    # Print summary
    print()
    print("=" * 70)
    print("B7 DOWN-Momentum / Goldilocks Symmetry Results")
    print("=" * 70)
    
    accuracy = aggregated.get('directional_accuracy', {})
    sharpe = aggregated.get('sharpe_ratio', {})
    trades = aggregated.get('trade_count', {})
    
    print(f"\nDirectional Accuracy:")
    print(f"  Mean: {accuracy.get('mean', 0):.4f} ({accuracy.get('mean', 0) * 100:.2f}%)")
    print(f"  Median: {accuracy.get('median', 0):.4f}")
    print(f"  Stations: {accuracy.get('count', 0)}")
    
    print(f"\nSharpe Ratio:")
    print(f"  Mean: {sharpe.get('mean', 0):.4f}")
    print(f"  Median: {sharpe.get('median', 0):.4f}")
    
    print(f"\nTrade Count:")
    print(f"  Mean: {trades.get('mean', 0):.1f} trades/station")
    print(f"  Median: {trades.get('median', 0):.1f}")
    
    print(f"\nStation Coverage:")
    print(f"  Total stations: {aggregated.get('station_count', 0)}")
    print(f"  Successful: {aggregated.get('successful_stations', 0)}")
    
    # Write summary markdown
    summary_path = os.path.join(output_dir, f"b7_down_momentum_goldilocks_summary_{timestamp}.md")
    with open(summary_path, 'w') as f:
        f.write(f"# B7 DOWN-Momentum / Goldilocks Symmetry Results\n\n")
        f.write(f"**Generated:** {datetime.now(timezone.utc).isoformat()}\n\n")
        f.write(f"## Summary Metrics\n\n")
        f.write(f"| Metric | Value |\n")
        f.write(f"|--------|-------|\n")
        f.write(f"| Directional Accuracy (mean) | {accuracy.get('mean', 0):.4f} ({accuracy.get('mean', 0) * 100:.2f}%) |\n")
        f.write(f"| Directional Accuracy (median) | {accuracy.get('median', 0):.4f} |\n")
        f.write(f"| Sharpe Ratio (mean) | {sharpe.get('mean', 0):.4f} |\n")
        f.write(f"| Sharpe Ratio (median) | {sharpe.get('median', 0):.4f} |\n")
        f.write(f"| Trade Count (mean) | {trades.get('mean', 0):.1f} |\n")
        f.write(f"| Station Coverage | {aggregated.get('successful_stations', 0)} / {aggregated.get('station_count', 0)} |\n")
        f.write(f"\n## Station Breakdown\n\n")
        f.write(f"| Station | Accuracy | Sharpe | Trades |\n")
        f.write(f"|---------|----------|--------|--------|\n")
        for station, station_results in sorted(aggregated.get('station_breakout', {}).items()):
            if 'error' not in station_results:
                acc = station_results.get('accuracy', 0)
                sh = station_results.get('sharpe_ratio', 0)
                tc = station_results.get('trade_count', 0)
                f.write(f"| {station} | {acc:.4f} ({acc*100:.2f}%) | {sh:.4f} | {tc} |\n")
    
    print(f"\nSummary written: {summary_path}")
    print(f"\n✅ B7 DOWN-Momentum / Goldilocks Symmetry Complete!")


if __name__ == "__main__":
    main()
