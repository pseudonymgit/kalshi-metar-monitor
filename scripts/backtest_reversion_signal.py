#!/usr/bin/env python3
"""
Backtest the reversion→direction signal (Phase 2 trade signal).

This measures the actual Gray Room-validated signal:
- Reversion=0 → UP direction (97.5% accuracy)
- Reversion=1 → DOWN direction (69.8% accuracy)

The backtest compares the reversion flag against the NEXT epoch's behavior:
- Does reversion=0 predict the next epoch goes UP?
- Does reversion=1 predict the next epoch goes DOWN?
"""

import sqlite3
import json
import os
import sys
from datetime import datetime
from typing import Dict, List, Optional, Any
from collections import defaultdict

# Add core module to path
script_dir = os.path.dirname(os.path.abspath(__file__))
workspace_dir = os.path.dirname(script_dir)
if script_dir not in sys.path:
    sys.path.insert(0, script_dir)
if workspace_dir not in sys.path:
    sys.path.insert(0, workspace_dir)


def get_reversion_direction_accuracy(db_path: str) -> Dict[str, Any]:
    """Calculate reversion→direction accuracy from settlement epochs."""
    
    conn = sqlite3.connect(db_path, timeout=30)
    cursor = conn.cursor()
    
    print("=" * 70)
    print("REVERSION→DIRECTION BACKTEST")
    print("=" * 70)
    print()
    print("Testing: Does reversion flag predict next epoch's direction?")
    print()
    
    # Get all epochs with reversion flags
    cursor.execute("""
        SELECT 
            id, station, market_type, local_trading_date,
            settlement_bucket, prior_settlement_bucket,
            reversion_occurred
        FROM settlement_epochs
        ORDER BY station, market_type, local_trading_date, id
    """)
    
    all_epochs = cursor.fetchall()
    columns = ['id', 'station', 'market_type', 'local_trading_date', 
               'settlement_bucket', 'prior_settlement_bucket', 'reversion_occurred']
    
    # Group by station/market_type
    station_market = defaultdict(list)
    for row in all_epochs:
        key = (row[0], row[1])  # Actually station, market_type
        # Fix: row[1] is station, row[2] is market_type
        key = (row[1], row[2])  # station, market_type
        station_market[key].append(dict(zip(columns, row)))
    
    # Sort each group by date
    for key in station_market:
        station_market[key].sort(key=lambda e: (e['local_trading_date'], e['id']))
    
    # For each epoch, predict direction based on reversion flag and compare to next epoch
    predictions = []
    
    for (station, market_type), epochs in station_market.items():
        for i, epoch in enumerate(epochs):
            if i == 0:
                continue  # Skip first epoch (no prior)
            
            current_reversion = epoch['reversion_occurred']
            
            # Predict direction: reversion=0 → UP, reversion=1 → DOWN
            predicted_direction = 'UP' if current_reversion == 0 else 'DOWN'
            
            # Get next epoch to compare
            if i + 1 < len(epochs):
                next_epoch = epochs[i + 1]
                
                # Calculate next epoch's direction
                current_bucket = epoch['settlement_bucket']
                next_bucket = next_epoch['settlement_bucket']
                
                if next_bucket > current_bucket:
                    actual_direction = 'UP'
                elif next_bucket < current_bucket:
                    actual_direction = 'DOWN'
                else:
                    actual_direction = 'FLAT'
                
                # Did prediction match?
                prediction_correct = (predicted_direction == actual_direction)
                
                predictions.append({
                    'station': station,
                    'market_type': market_type,
                    'date': epoch['local_trading_date'],
                    'reversion': current_reversion,
                    'predicted': predicted_direction,
                    'actual': actual_direction,
                    'correct': prediction_correct,
                })
    
    conn.close()
    
    # Calculate accuracy
    if not predictions:
        return {'error': 'No predictions generated'}
    
    total = len(predictions)
    correct = sum(1 for p in predictions if p['correct'])
    accuracy = correct / total if total > 0 else 0
    
    # Per-station breakdown
    station_stats = defaultdict(lambda: {'total': 0, 'correct': 0})
    for p in predictions:
        station = p['station']
        station_stats[station]['total'] += 1
        if p['correct']:
            station_stats[station]['correct'] += 1
    
    # Per-market-type breakdown
    mt_stats = defaultdict(lambda: {'total': 0, 'correct': 0})
    for p in predictions:
        mt = p['market_type']
        mt_stats[mt]['total'] += 1
        if p['correct']:
            mt_stats[mt]['correct'] += 1
    
    # Reversion flag breakdown
    reversion_stats = {
        'reversion_0': {'total': 0, 'correct': 0},
        'reversion_1': {'total': 0, 'correct': 0},
    }
    for p in predictions:
        rev = p['reversion']
        if rev == 0:
            reversion_stats['reversion_0']['total'] += 1
            if p['correct']:
                reversion_stats['reversion_0']['correct'] += 1
        elif rev == 1:
            reversion_stats['reversion_1']['total'] += 1
            if p['correct']:
                reversion_stats['reversion_1']['correct'] += 1
    
    return {
        'total_predictions': total,
        'correct_predictions': correct,
        'directional_accuracy': accuracy,
        'station_stats': dict(station_stats),
        'market_type_stats': dict(mt_stats),
        'reversion_stats': reversion_stats,
    }


def calculate_reversion_accuracy(db_path: str) -> Dict[str, Any]:
    """Calculate reversion accuracy: does reversion=0 mean next epoch goes UP?"""
    
    conn = sqlite3.connect(db_path, timeout=30)
    cursor = conn.cursor()
    
    # Get all epochs
    cursor.execute("""
        SELECT 
            id, station, market_type, local_trading_date,
            settlement_bucket, prior_settlement_bucket,
            reversion_occurred
        FROM settlement_epochs
        ORDER BY station, market_type, local_trading_date, id
    """)
    
    all_epochs = cursor.fetchall()
    columns = ['id', 'station', 'market_type', 'local_trading_date', 
               'settlement_bucket', 'prior_settlement_bucket', 'reversion_occurred']
    
    # Group by station/market_type
    station_market = defaultdict(list)
    for row in all_epochs:
        key = (row[1], row[2])  # station, market_type
        station_market[key].append(dict(zip(columns, row)))
    
    # Sort each group by date
    for key in station_market:
        station_market[key].sort(key=lambda e: (e['local_trading_date'], e['id']))
    
    # For each epoch, check if reversion flag predicts NEXT epoch going UP
    # Gray Room finding: reversion=0 → UP 97.5% of time
    predictions = []
    
    for (station, market_type), epochs in station_market.items():
        for i, epoch in enumerate(epochs):
            if i + 1 >= len(epochs):
                continue  # Skip last epoch
            
            current_reversion = epoch['reversion_occurred']
            next_epoch = epochs[i + 1]
            
            # Next epoch goes UP if its settlement_bucket > current epoch's
            current_bucket = epoch['settlement_bucket']
            next_bucket = next_epoch['settlement_bucket']
            next_goes_up = (next_bucket > current_bucket)
            
            # Gray Room prediction: reversion=0 → next goes UP
            predicted = (current_reversion == 0)
            actual = next_goes_up
            correct = (predicted == actual)
            
            predictions.append({
                'station': station,
                'market_type': market_type,
                'date': epoch['local_trading_date'],
                'reversion': current_reversion,
                'predicted_goes_up': predicted,
                'actually_goes_up': actual,
                'correct': correct,
            })
    
    conn.close()
    
    # Calculate accuracy
    if not predictions:
        return {'error': 'No predictions generated'}
    
    total = len(predictions)
    correct = sum(1 for p in predictions if p['correct'])
    accuracy = correct / total if total > 0 else 0
    
    # Per-station breakdown
    station_stats = defaultdict(lambda: {'total': 0, 'correct': 0})
    for p in predictions:
        station = p['station']
        station_stats[station]['total'] += 1
        if p['correct']:
            station_stats[station]['correct'] += 1
    
    # Per-market-type breakdown
    mt_stats = defaultdict(lambda: {'total': 0, 'correct': 0})
    for p in predictions:
        mt = p['market_type']
        mt_stats[mt]['total'] += 1
        if p['correct']:
            mt_stats[mt]['correct'] += 1
    
    # Reversion flag breakdown
    reversion_stats = {
        'reversion_0': {'total': 0, 'correct': 0},
        'reversion_1': {'total': 0, 'correct': 0},
    }
    for p in predictions:
        rev = p['reversion']
        if rev == 0:
            reversion_stats['reversion_0']['total'] += 1
            if p['correct']:
                reversion_stats['reversion_0']['correct'] += 1
        elif rev == 1:
            reversion_stats['reversion_1']['total'] += 1
            if p['correct']:
                reversion_stats['reversion_1']['correct'] += 1
    
    return {
        'total_predictions': total,
        'correct_predictions': correct,
        'directional_accuracy': accuracy,
        'station_stats': dict(station_stats),
        'market_type_stats': dict(mt_stats),
        'reversion_stats': reversion_stats,
    }


def main():
    db_path = "/home/node/.openclaw/workspace/prototypes/weather-engine-source/data/metar_backfill.db"
    
    if not os.path.exists(db_path):
        print(f"ERROR: Database not found: {db_path}")
        return
    
    print("Running reversion→direction backtest...")
    print()
    
    # Method 1: Simple reversion prediction
    print("=" * 70)
    print("METHOD 1: Reversion Flag Prediction")
    print("=" * 70)
    print(" Prediction: reversion=0 → next epoch goes UP")
    print(" Prediction: reversion=1 → next epoch goes DOWN or stays flat")
    print()
    
    result1 = calculate_reversion_accuracy(db_path)
    
    print(f"Total predictions: {result1['total_predictions']}")
    print(f"Correct predictions: {result1['correct_predictions']}")
    print(f"Accuracy: {result1['directional_accuracy']:.2%}")
    print()
    
    # Show per-station
    print("STATION BREAKDOWN:")
    print("-" * 50)
    print(f"{'Station':<8} {'Total':>8} {'Correct':>8} {'Accuracy':>10}")
    print("-" * 36)
    
    for station in sorted(result1['station_stats'].keys()):
        stats = result1['station_stats'][station]
        acc = stats['correct'] / stats['total'] if stats['total'] > 0 else 0
        print(f"{station:<8} {stats['total']:>8} {stats['correct']:>8} {acc:>10.2%}")
    
    print()
    
    # Show reversion breakdown
    print("REVERSION FLAG BREAKDOWN:")
    print("-" * 50)
    
    rev0 = result1['reversion_stats']['reversion_0']
    rev0_acc = rev0['correct'] / rev0['total'] if rev0['total'] > 0 else 0
    print(f"  reversion=0: {rev0['correct']:>6}/{rev0['total']:>6} = {rev0_acc:.2%} (prediction: next goes UP)")
    
    rev1 = result1['reversion_stats']['reversion_1']
    rev1_acc = rev1['correct'] / rev1['total'] if rev1['total'] > 0 else 0
    print(f"  reversion=1: {rev1['correct']:>6}/{rev1['total']:>6} = {rev1_acc:.2%} (prediction: next doesn't go UP)")
    
    print()
    
    # Method 2: Next-epoch direction comparison
    print("=" * 70)
    print("METHOD 2: Next-Epoch Direction Comparison")
    print("=" * 70)
    print(" Prediction: reversion=0 → next direction is UP")
    print(" Prediction: reversion=1 → next direction is DOWN")
    print()
    
    result2 = get_reversion_direction_accuracy(db_path)
    
    print(f"Total predictions: {result2['total_predictions']}")
    print(f"Correct predictions: {result2['correct_predictions']}")
    print(f"Accuracy: {result2['directional_accuracy']:.2%}")
    print()
    
    # Show per-station
    print("STATION BREAKDOWN:")
    print("-" * 50)
    print(f"{'Station':<8} {'Total':>8} {'Correct':>8} {'Accuracy':>10}")
    print("-" * 36)
    
    for station in sorted(result2['station_stats'].keys()):
        stats = result2['station_stats'][station]
        acc = stats['correct'] / stats['total'] if stats['total'] > 0 else 0
        print(f"{station:<8} {stats['total']:>8} {stats['correct']:>8} {acc:>10.2%}")
    
    print()
    
    # Show reversion breakdown
    print("REVERSION FLAG BREAKDOWN:")
    print("-" * 50)
    
    rev0 = result2['reversion_stats']['reversion_0']
    rev0_acc = rev0['correct'] / rev0['total'] if rev0['total'] > 0 else 0
    print(f"  reversion=0: {rev0['correct']:>6}/{rev0['total']:>6} = {rev0_acc:.2%} (prediction: next direction is UP)")
    
    rev1 = result2['reversion_stats']['reversion_1']
    rev1_acc = rev1['correct'] / rev1['total'] if rev1['total'] > 0 else 0
    print(f"  reversion=1: {rev1['correct']:>6}/{rev1['total']:>6} = {rev1_acc:.2%} (prediction: next direction is DOWN)")
    
    print()
    
    # Final verdict
    print("=" * 70)
    print("FINAL VERDICT")
    print("=" * 70)
    print()
    
    threshold = 0.58  # Gray Room threshold
    
    method1_pass = result1['directional_accuracy'] >= threshold
    method2_pass = result2['directional_accuracy'] >= threshold
    
    print("METHOD 1 (Reversion Flag Prediction):")
    print(f"  Accuracy: {result1['directional_accuracy']:.2%}")
    print(f"  {'✓ PASS' if method1_pass else '✗ FAIL'} ≥ {threshold:.0%}")
    print()
    
    print("METHOD 2 (Next-Epoch Direction):")
    print(f"  Accuracy: {result2['directional_accuracy']:.2%}")
    print(f"  {'✓ PASS' if method2_pass else '✗ FAIL'} ≥ {threshold:.0%}")
    print()
    
    if method1_pass or method2_pass:
        print("✓ REVERSION SIGNAL CLEARS THRESHOLDS")
        print()
        print("RECOMMENDATION: PROCEED WITH TRADE SIGNAL IMPLEMENTATION")
    else:
        print("✗ REVERSION SIGNAL DOES NOT CLEAR THRESHOLDS")
        print()
        print("RECOMMENDATION: NEED MORE DATA OR FEATURE IMPROVEMENT")
    
    print()
    
    # Save results
    output_path = "/tmp/p3_backtest_reversion_results.json"
    output = {
        'method1': result1,
        'method2': result2,
        'threshold': threshold,
        'recommendation': "PROCEED" if (method1_pass or method2_pass) else "REVIEW",
    }
    
    with open(output_path, 'w') as f:
        json.dump(output, f, indent=2)
    
    print(f"Results saved to: {output_path}")


if __name__ == "__main__":
    main()
