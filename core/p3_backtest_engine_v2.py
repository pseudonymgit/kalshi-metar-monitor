# CHANGELOG (last 10 broad changes):
# 1. [2026-07-05 R4-1.1: Fix P&L mark-to-market + thread-safe price cache]
#


"""
Phase 3 Backtest Engine v2

Redesigned backtest with proper ground truth comparison.
Measures meaningful outcomes:
1. Next-epoch direction accuracy: did temperature go up/down/flat in the following epoch?
2. Reversion prediction: will a reversion occur after this epoch?
3. Terminal state: what was the final settlement value vs the prediction?
4. Per market_type (HIGH vs LOW) analysis

The previous backtest was fundamentally broken - it compared against "settlement_bucket > prior_settlement_bucket"
which is always "up" since settlement_bucket is the running daily max. This version compares against
meaningful trading signals.
"""

import sqlite3
import json
import os
import math
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Tuple
from collections import defaultdict
import statistics
import sys
import inspect

# Add core module to path - handle both script and module execution
script_dir = os.path.dirname(os.path.abspath(inspect.getfile(inspect.currentframe())))
workspace_dir = os.path.dirname(script_dir)
if script_dir not in sys.path:
    sys.path.insert(0, script_dir)
if workspace_dir not in sys.path:
    sys.path.insert(0, workspace_dir)

from p3_feature_extractor import extract_features_from_epoch, FeatureVector
from p3_match_engine import find_similar_epochs, get_top_analogs
from p3_trajectory_tracer import trace_all_trajectories
from p3_calibration_engine import calculate_confidence
from p3_output_formatter import create_prediction
import p3_scheduler as p3sch
from .sqlite_utils import get_sqlite_connection, get_readonly_sqlite_connection


# =============================================================================
# Data Classes
# =============================================================================

@dataclass
class PredictionResult:
    """A single prediction result with ground truth comparison."""
    epoch_id: int
    station: str
    market_type: Optional[str]
    query_date: str
    query_bucket: int
    prior_bucket: Optional[int]
    
    # Prediction
    predicted_direction: str  # 'up', 'down', 'flat'
    predicted_bucket: int
    top_analogs: List[Dict]
    analog_count: int
    confidence_score: float
    confidence_band: str
    
    # Ground truth (next epoch behavior)
    next_epoch_bucket: Optional[int]
    next_epoch_direction: Optional[str]  # 'up', 'down', 'flat'
    
    # Reversion ground truth
    reversion_occurred: int
    reversion_predicted: bool
    
    # Terminal state
    terminal_state_reached: int
    terminal_bucket: Optional[int]
    
    # Results
    directional_correct: bool
    reversion_correct: Optional[bool]  # None if no reversion in next epoch
    magnitude_error: float
    distance_from_prediction: int
    
    # Trajectory analysis
    analog_trajectory: List[int]
    trajectory_direction: Optional[str]
    trajectory_match: Optional[bool]
    
    timestamp_utc: str = field(default_factory=lambda: datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))


@dataclass
class BacktestSummary:
    """Aggregated backtest statistics."""
    total_predictions: int
    total_valid_analogs: int
    
    # Directional accuracy (next-epoch direction)
    directional_correct: int
    directional_accuracy: float
    
    # Reversion prediction
    reversion_total: int
    reversion_correct: int
    reversion_accuracy: float
    reversion_precision: float
    reversion_recall: float
    
    # Magnitude error
    avg_magnitude_error: float
    median_magnitude_error: float
    min_magnitude_error: float
    max_magnitude_error: float
    
    # Confidence calibration
    high_conf_correct: int
    high_conf_total: int
    high_conf_accuracy: float
    low_conf_correct: int
    low_conf_total: int
    low_conf_accuracy: float
    confidence_calibration: float
    
    # Per-station breakdown
    station_stats: Dict[str, Dict[str, Any]]
    
    # Per-market-type breakdown
    market_type_stats: Dict[str, Dict[str, Any]]
    
    # Per-confidence-band breakdown
    confidence_band_stats: Dict[str, Dict[str, Any]]
    
    # Signal validity
    valid_predictions_count: int
    invalid_predictions_count: int
    
    # Trajectory analysis
    trajectory_matches: int
    trajectory_mismatches: int
    no_trajectory_data: int
    
    # Timestamps
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    generated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))


# =============================================================================
# Backtest Engine
# =============================================================================

class Phase3BacktestEngine:
    """Comprehensive backtesting engine for Phase 3 predictions."""
    
    MIN_ANALOGS = 3  # Minimum analogs for statistical validity
    MIN_CONFIDENCE_THRESHOLD = 0.6  # Threshold for "high confidence"
    DIRECTIONAL_THRESHOLD = 0.58  # Target: ≥58% directional accuracy
    CALIBRATION_THRESHOLD = 0.65  # Target: ≥65% confidence calibration
    
    def __init__(self, db_path: str):
        """Initialize backtest engine with database path."""
        self.db_path = db_path
        self.conn = get_sqlite_connection(db_path, timeout=10)
        self.conn.row_factory = sqlite3.Row
        self.cursor = self.conn.cursor()
        
    def close(self):
        """Close database connection."""
        if self.conn:
            self.conn.close()
    
    def get_all_settlement_epochs(self) -> List[Dict[str, Any]]:
        """Get all settlement epochs from database."""
        self.cursor.execute("""
            SELECT 
                id, station, market_type, local_trading_date,
                settlement_bucket, prior_settlement_bucket,
                settlement_timestamp_utc, settlement_jump_magnitude,
                epoch_status, epoch_close_reason, epoch_close_timestamp_utc,
                reversion_occurred, first_reversion_timestamp_utc,
                max_excursion_above_settlement,
                duration_at_or_above_settlement_seconds,
                duration_strictly_above_settlement_seconds,
                terminal_state_reached,
                settlement_transition_event_id, last_transition_event_id,
                last_transition_timestamp_utc, last_transition_temp_f
            FROM settlement_epochs
            ORDER BY local_trading_date ASC, id ASC
        """)
        
        columns = [desc[0] for desc in self.cursor.description]
        return [dict(zip(columns, row)) for row in self.cursor.fetchall()]
    
    def get_closed_epochs_before(self, station: str, local_date: str, 
                                   market_type: Optional[str], limit: int = 100) -> List[Dict]:
        """Get closed epochs for a station before a specific date."""
        self.cursor.execute("""
            SELECT 
                id, station, market_type, local_trading_date,
                settlement_bucket, prior_settlement_bucket,
                settlement_timestamp_utc, settlement_jump_magnitude,
                epoch_status, epoch_close_reason, epoch_close_timestamp_utc,
                reversion_occurred, first_reversion_timestamp_utc,
                max_excursion_above_settlement,
                duration_at_or_above_settlement_seconds,
                duration_strictly_above_settlement_seconds,
                terminal_state_reached,
                settlement_transition_event_id, last_transition_event_id,
                last_transition_timestamp_utc, last_transition_temp_f
            FROM settlement_epochs
            WHERE station = ?
              AND ((market_type IS NULL AND ? IS NULL) OR market_type = ?)
              AND epoch_status = 'closed'
              AND local_trading_date < ?
            ORDER BY local_trading_date DESC, id DESC
            LIMIT ?
        """, (station, market_type, market_type, local_date, limit))
        
        columns = [desc[0] for desc in self.cursor.description]
        return [dict(zip(columns, row)) for row in self.cursor.fetchall()]
    
    def determine_direction(self, bucket: int, prior_bucket: Optional[int]) -> str:
        """Determine direction based on bucket change."""
        if prior_bucket is None:
            return 'flat'
        if bucket > prior_bucket:
            return 'up'
        elif bucket < prior_bucket:
            return 'down'
        else:
            return 'flat'
    
    def get_next_epoch_info(self, station: str, market_type: str, 
                            local_date: str, epoch_id: int) -> Optional[Dict]:
        """Get information about the next epoch for the same station/market_type."""
        self.cursor.execute("""
            SELECT 
                id, local_trading_date, settlement_bucket, prior_settlement_bucket,
                reversion_occurred, terminal_state_reached
            FROM settlement_epochs
            WHERE station = ? AND market_type = ?
              AND (local_trading_date > ? OR (local_trading_date = ? AND id > ?))
            ORDER BY local_trading_date ASC, id ASC
            LIMIT 1
        """, (station, market_type, local_date, local_date, epoch_id))
        
        row = self.cursor.fetchone()
        if row:
            return {
                'id': row[0],
                'local_trading_date': row[1],
                'settlement_bucket': row[2],
                'prior_settlement_bucket': row[3],
                'reversion_occurred': row[4],
                'terminal_state_reached': row[5]
            }
        return None
    
    def simulate_prediction(self, query_epoch: Dict, corpus: List[Dict], 
                          next_epoch: Optional[Dict]) -> PredictionResult:
        """
        Simulate a Phase 3 prediction for a query epoch using corpus of prior epochs.
        
        This simulates what Phase 3 would have predicted at the time of the query epoch.
        """
        try:
            # Extract features from query epoch
            query_features = extract_features_from_epoch(query_epoch)
            
            # Find analogs in corpus
            match_result = find_similar_epochs(query_features, corpus)
            
            # Get top analogs
            top_analogs = get_top_analogs(match_result, k=10)
            analog_count = len(top_analogs)
            
            # Check if we have enough analogs for statistical validity
            has_min_analogs = analog_count >= self.MIN_ANALOGS
            
            # Calculate confidence
            if top_analogs:
                match_scores = [m.match_score for m in top_analogs]
                n = len(match_scores)
                
                if n >= 2:
                    mean_score = sum(match_scores) / n
                    variance = sum((s - mean_score) ** 2 for s in match_scores) / n
                    sigma = variance ** 0.5
                    mu = mean_score
                else:
                    sigma = 0.0
                    mu = 1.0
                
                # Simplified kurtosis calculation
                if n >= 4:
                    mean = sum(match_scores) / n
                    m2 = sum((s - mean) ** 2 for s in match_scores) / n
                    m4 = sum((s - mean) ** 4 for s in match_scores) / n
                    excess_kurtosis = (m4 / m2 ** 2) - 3 if m2 > 0 else 0.0
                else:
                    excess_kurtosis = 0.0
                
                confidence = calculate_confidence(
                    n=n,
                    excess_kurtosis=excess_kurtosis,
                    sigma=sigma,
                    mu=mu,
                    brier_score=0.2,  # Placeholder
                    delta_t_hours=0.0,
                    p_up=0.6,
                    p_down=0.4,
                    outcomes=match_scores,
                )
            else:
                confidence = calculate_confidence(
                    n=0, excess_kurtosis=0, sigma=0.0, mu=1.0,
                    brier_score=0.5, delta_t_hours=0.0, p_up=0.5, p_down=0.5,
                    outcomes=[0.5],
                )
            
            # Determine predicted direction (next-epoch direction)
            if has_min_analogs and top_analogs:
                # Use average of analog prior buckets to predict direction
                analog_prior_buckets = [a.epoch_data.get('prior_settlement_bucket') 
                                       for a in top_analogs if a.epoch_data.get('prior_settlement_bucket') is not None]
                
                if analog_prior_buckets:
                    avg_analog_prior = statistics.mean(analog_prior_buckets)
                    avg_analog_settlement = statistics.mean([a.epoch_data.get('settlement_bucket', 0) 
                                                           for a in top_analogs])
                    
                    if avg_analog_settlement > avg_analog_prior:
                        predicted_direction = 'up'
                        predicted_bucket = query_epoch.get('settlement_bucket', 0) + 5
                    elif avg_analog_settlement < avg_analog_prior:
                        predicted_direction = 'down'
                        predicted_bucket = query_epoch.get('settlement_bucket', 0) - 5
                    else:
                        predicted_direction = 'flat'
                        predicted_bucket = query_epoch.get('settlement_bucket', 0)
                else:
                    predicted_direction = 'flat'
                    predicted_bucket = query_epoch.get('settlement_bucket', 0)
            else:
                predicted_direction = 'flat'
                predicted_bucket = query_epoch.get('settlement_bucket', 0)
            
            # Get actual next-epoch direction
            next_epoch_bucket = None
            next_epoch_direction = None
            if next_epoch:
                next_epoch_bucket = next_epoch.get('settlement_bucket')
                prior_for_next = next_epoch.get('prior_settlement_bucket')
                if prior_for_next is not None:
                    next_epoch_direction = self.determine_direction(next_epoch_bucket, prior_for_next)
            
            # Reversion prediction - predict if reversion will occur in next epoch
            reversion_occurred = query_epoch.get('reversion_occurred', 0)
            reversion_predicted = (predicted_direction == 'down')  # Down prediction suggests reversion
            
            # Terminal state
            terminal_state_reached = query_epoch.get('terminal_state_reached', 0)
            terminal_bucket = query_epoch.get('settlement_bucket')
            
            # Calculate results
            directional_correct = (predicted_direction == next_epoch_direction) if next_epoch_direction else False
            magnitude_error = abs(next_epoch_bucket - predicted_bucket) if next_epoch_bucket else 0
            distance_from_prediction = abs(next_epoch_bucket - predicted_bucket) if next_epoch_bucket else 0
            
            # Reversion correctness
            reversion_correct = None
            if next_epoch:
                next_reversion = next_epoch.get('reversion_occurred', 0)
                reversion_correct = (reversion_predicted == (next_reversion == 1))
            
            # Calculate trajectory from analogs
            analog_trajectory = []
            trajectory_direction = None
            trajectory_match = None
            
            if has_min_analogs:
                # Get actual trajectories from analog epochs
                for analog in top_analogs:
                    analog_id = analog.matched_epoch_id
                    # Find what happened after this analog epoch
                    self.cursor.execute("""
                        SELECT settlement_bucket, prior_settlement_bucket
                        FROM settlement_epochs
                        WHERE id = ? AND epoch_status = 'closed'
                    """, (analog_id,))
                    row = self.cursor.fetchone()
                    if row:
                        analog_trajectory.append(row[0])
                
                if analog_trajectory:
                    # Calculate trajectory direction
                    if len(analog_trajectory) >= 2:
                        if analog_trajectory[-1] > analog_trajectory[0]:
                            trajectory_direction = 'up'
                        elif analog_trajectory[-1] < analog_trajectory[0]:
                            trajectory_direction = 'down'
                        else:
                            trajectory_direction = 'flat'
                        
                        # Check if trajectory matches prediction
                        trajectory_match = (trajectory_direction == predicted_direction)
            
            return PredictionResult(
                epoch_id=query_epoch.get('id', 0),
                station=query_epoch.get('station', 'UNKNOWN'),
                market_type=query_epoch.get('market_type'),
                query_date=query_epoch.get('local_trading_date', ''),
                query_bucket=query_epoch.get('settlement_bucket', 0),
                prior_bucket=query_epoch.get('prior_settlement_bucket'),
                predicted_direction=predicted_direction,
                predicted_bucket=predicted_bucket,
                top_analogs=[{
                    'id': a.matched_epoch_id,
                    'score': a.match_score,
                    'date': a.epoch_data.get('local_trading_date'),
                } for a in top_analogs],
                analog_count=analog_count,
                confidence_score=confidence.final_score,
                confidence_band=confidence.band,
                next_epoch_bucket=next_epoch_bucket,
                next_epoch_direction=next_epoch_direction,
                reversion_occurred=reversion_occurred,
                reversion_predicted=reversion_predicted,
                reversion_correct=reversion_correct,
                terminal_state_reached=terminal_state_reached,
                terminal_bucket=terminal_bucket,
                directional_correct=directional_correct,
                magnitude_error=magnitude_error,
                distance_from_prediction=distance_from_prediction,
                analog_trajectory=analog_trajectory,
                trajectory_direction=trajectory_direction,
                trajectory_match=trajectory_match,
            )
            
        except Exception as e:
            print(f"Error simulating prediction for epoch {query_epoch.get('id')}: {e}")
            return None
    
    def run_backtest(self, start_date: str = None, end_date: str = None) -> List[PredictionResult]:
        """
        Run backtest on historical settlement epochs.
        
        For each closed epoch (after a warmup period), simulate what prediction
        would have been made at that time using only prior history, and compare
        against the next epoch's actual behavior.
        """
        all_epochs = self.get_all_settlement_epochs()
        
        if not all_epochs:
            print("No settlement epochs found in database!")
            return []
        
        # Group epochs by station and market_type
        station_market_epochs = defaultdict(list)
        for epoch in all_epochs:
            key = (epoch.get('station'), epoch.get('market_type'))
            station_market_epochs[key].append(epoch)
        
        # Run backtest for each station/market_type combination
        results = []
        
        for (station, market_type), epochs in station_market_epochs.items():
            # Sort by date
            epochs.sort(key=lambda e: (e.get('local_trading_date', ''), e.get('id', 0)))
            
            # Warmup period: need at least 30 prior epochs for meaningful analogs
            warmup_period = 30
            
            # Process each epoch as a query
            for i in range(warmup_period, len(epochs)):
                query_epoch = epochs[i]
                prior_epochs = epochs[:i]  # All epochs before this one
                
                # Filter to closed epochs only for corpus
                closed_prior_epochs = [e for e in prior_epochs if e.get('epoch_status') == 'closed']
                
                if len(closed_prior_epochs) >= self.MIN_ANALOGS:
                    # Get next epoch info for ground truth
                    next_epoch = self.get_next_epoch_info(
                        station, market_type,
                        query_epoch.get('local_trading_date'),
                        query_epoch.get('id')
                    )
                    
                    result = self.simulate_prediction(query_epoch, closed_prior_epochs, next_epoch)
                    if result:
                        results.append(result)
        
        return results
    
    def calculate_summary(self, results: List[PredictionResult]) -> BacktestSummary:
        """Calculate comprehensive summary statistics."""
        if not results:
            return BacktestSummary(total_predictions=0, total_valid_analogs=0)
        
        # Basic counts
        total_predictions = len(results)
        valid_analogs = [r for r in results if r.analog_count >= self.MIN_ANALOGS]
        total_valid_analogs = len(valid_analogs)
        
        # Directional accuracy (next-epoch direction)
        directional_correct = sum(1 for r in results if r.directional_correct)
        directional_accuracy = directional_correct / total_predictions if total_predictions > 0 else 0
        
        # Reversion prediction
        reversion_results = [r for r in results if r.reversion_correct is not None]
        reversion_total = len(reversion_results)
        reversion_correct = sum(1 for r in reversion_results if r.reversion_correct)
        reversion_accuracy = reversion_correct / reversion_total if reversion_total > 0 else 0
        
        # Precision/Recall for reversion
        predicted_reversion = sum(1 for r in results if r.reversion_predicted)
        true_reversion = sum(1 for r in results if r.reversion_occurred == 1)
        true_positive = sum(1 for r in reversion_results if r.reversion_predicted and r.reversion_correct)
        
        reversion_precision = true_positive / predicted_reversion if predicted_reversion > 0 else 0
        reversion_recall = true_positive / true_reversion if true_reversion > 0 else 0
        
        # Magnitude error (for epochs with next_epoch_bucket)
        with_next = [r for r in results if r.next_epoch_bucket is not None]
        magnitude_errors = [r.magnitude_error for r in with_next]
        avg_magnitude_error = statistics.mean(magnitude_errors) if magnitude_errors else 0
        median_magnitude_error = statistics.median(magnitude_errors) if magnitude_errors else 0
        min_magnitude_error = min(magnitude_errors) if magnitude_errors else 0
        max_magnitude_error = max(magnitude_errors) if magnitude_errors else 0
        
        # Confidence calibration
        high_conf = [r for r in results if r.confidence_score >= self.MIN_CONFIDENCE_THRESHOLD]
        low_conf = [r for r in results if r.confidence_score < self.MIN_CONFIDENCE_THRESHOLD]
        
        high_conf_correct = sum(1 for r in high_conf if r.directional_correct)
        high_conf_total = len(high_conf)
        high_conf_accuracy = high_conf_correct / high_conf_total if high_conf_total > 0 else 0
        
        low_conf_correct = sum(1 for r in low_conf if r.directional_correct)
        low_conf_total = len(low_conf)
        low_conf_accuracy = low_conf_correct / low_conf_total if low_conf_total > 0 else 0
        
        # Confidence calibration: high confidence should have higher accuracy than low
        confidence_calibration = high_conf_accuracy > low_conf_accuracy
        
        # Per-station stats
        station_stats = defaultdict(lambda: {
            'total': 0, 'correct': 0, 'accuracy': 0,
            'avg_magnitude_error': 0, 'reversion_correct': 0, 'reversion_total': 0
        })
        
        for r in results:
            station = r.station
            station_stats[station]['total'] += 1
            if r.directional_correct:
                station_stats[station]['correct'] += 1
            if r.next_epoch_bucket:
                station_stats[station]['avg_magnitude_error'] += r.magnitude_error
            if r.reversion_correct is not None:
                station_stats[station]['reversion_total'] += 1
                if r.reversion_correct:
                    station_stats[station]['reversion_correct'] += 1
        
        for station in station_stats:
            stats = station_stats[station]
            if stats['total'] > 0:
                stats['accuracy'] = stats['correct'] / stats['total']
                stats['avg_magnitude_error'] = stats['avg_magnitude_error'] / stats['total']
            if stats['reversion_total'] > 0:
                stats['reversion_accuracy'] = stats['reversion_correct'] / stats['reversion_total']
        
        # Per-market-type stats
        market_type_stats = defaultdict(lambda: {
            'total': 0, 'correct': 0, 'accuracy': 0,
            'reversion_correct': 0, 'reversion_total': 0
        })
        
        for r in results:
            mt = r.market_type or 'NONE'
            market_type_stats[mt]['total'] += 1
            if r.directional_correct:
                market_type_stats[mt]['correct'] += 1
            if r.reversion_correct is not None:
                market_type_stats[mt]['reversion_total'] += 1
                if r.reversion_correct:
                    market_type_stats[mt]['reversion_correct'] += 1
        
        for mt in market_type_stats:
            stats = market_type_stats[mt]
            if stats['total'] > 0:
                stats['accuracy'] = stats['correct'] / stats['total']
            if stats['reversion_total'] > 0:
                stats['reversion_accuracy'] = stats['reversion_correct'] / stats['reversion_total']
        
        # Per-confidence-band stats
        confidence_band_stats = defaultdict(lambda: {
            'total': 0, 'correct': 0, 'accuracy': 0,
            'avg_score': 0, 'min_score': float('inf'), 'max_score': 0
        })
        
        for r in results:
            band = r.confidence_band
            confidence_band_stats[band]['total'] += 1
            if r.directional_correct:
                confidence_band_stats[band]['correct'] += 1
            confidence_band_stats[band]['avg_score'] += r.confidence_score
            confidence_band_stats[band]['min_score'] = min(
                confidence_band_stats[band]['min_score'], r.confidence_score
            )
            confidence_band_stats[band]['max_score'] = max(
                confidence_band_stats[band]['max_score'], r.confidence_score
            )
        
        for band in confidence_band_stats:
            stats = confidence_band_stats[band]
            if stats['total'] > 0:
                stats['accuracy'] = stats['correct'] / stats['total']
                stats['avg_score'] = stats['avg_score'] / stats['total']
                if stats['min_score'] == float('inf'):
                    stats['min_score'] = 0
                stats['min_score'] = round(stats['min_score'], 4)
                stats['max_score'] = round(stats['max_score'], 4)
        
        # Signal validity
        valid_predictions = len(valid_analogs)
        invalid_predictions = total_predictions - valid_predictions
        
        # Trajectory analysis
        trajectory_matches = sum(1 for r in results if r.trajectory_match)
        trajectory_mismatches = sum(1 for r in results if r.trajectory_match is False)
        no_trajectory_data = sum(1 for r in results if r.trajectory_match is None)
        
        # Date range
        dates = [r.query_date for r in results]
        start_date = min(dates) if dates else None
        end_date = max(dates) if dates else None
        
        return BacktestSummary(
            total_predictions=total_predictions,
            total_valid_analogs=total_valid_analogs,
            directional_correct=directional_correct,
            directional_accuracy=round(directional_accuracy, 4),
            reversion_total=reversion_total,
            reversion_correct=reversion_correct,
            reversion_accuracy=round(reversion_accuracy, 4),
            reversion_precision=round(reversion_precision, 4),
            reversion_recall=round(reversion_recall, 4),
            avg_magnitude_error=round(avg_magnitude_error, 2),
            median_magnitude_error=round(median_magnitude_error, 2),
            min_magnitude_error=round(min_magnitude_error, 2),
            max_magnitude_error=round(max_magnitude_error, 2),
            high_conf_correct=high_conf_correct,
            high_conf_total=high_conf_total,
            high_conf_accuracy=round(high_conf_accuracy, 4),
            low_conf_correct=low_conf_correct,
            low_conf_total=low_conf_total,
            low_conf_accuracy=round(low_conf_accuracy, 4) if low_conf_total > 0 else 0,
            confidence_calibration=confidence_calibration,
            station_stats=dict(station_stats),
            market_type_stats=dict(market_type_stats),
            confidence_band_stats=dict(confidence_band_stats),
            valid_predictions_count=valid_predictions,
            invalid_predictions_count=invalid_predictions,
            trajectory_matches=trajectory_matches,
            trajectory_mismatches=trajectory_mismatches,
            no_trajectory_data=no_trajectory_data,
            start_date=start_date,
            end_date=end_date,
        )


# =============================================================================
# Main Execution
# =============================================================================

def main():
    """Run Phase 3 backtest analysis."""
    print("=" * 80)
    print("PHASE 3 BACKTEST ENGINE v2 (REDESIGNED)")
    print("=" * 80)
    print()
    print("Measuring meaningful outcomes:")
    print("  1. Next-epoch direction: did temperature go up/down in the following epoch?")
    print("  2. Reversion prediction: will a reversion occur after this epoch?")
    print("  3. Terminal state: what was the final settlement value vs prediction?")
    print("  4. Per market_type (HIGH vs LOW) analysis")
    print()
    
    # Database paths to check
    db_paths = [
        "/home/node/.openclaw/workspace/prototypes/weather-engine-source-backup-2026-06-17/alerts-prod.db",
        "/home/node/.openclaw/workspace/prototypes/weather-engine-source-backup-20260627-1720/core/alerts.db",
        "/home/node/.openclaw/workspace/prototypes/weather-engine-source/core/alerts.db",
        "/home/node/.openclaw/workspace/prototypes/weather-engine-source/alerts.db",
    ]
    
    # Find a database with data
    db_path = None
    for path in db_paths:
        if os.path.exists(path):
            try:
                conn = get_sqlite_connection(path)
                cursor = conn.cursor()
                cursor.execute("SELECT COUNT(*) FROM settlement_epochs")
                count = cursor.fetchone()[0]
                conn.close()
                if count > 0:
                    db_path = path
                    print(f"Using database: {db_path} ({count} epochs)")
                    break
            except Exception as e:
                pass
    
    if not db_path:
        print("ERROR: No database with settlement epochs found!")
        print()
        print("Need data to backtest. Possible scenarios:")
        print("  1. Database not yet populated - run initial data ingestion")
        print("  2. Database path incorrect - check ALERT_DB_PATH environment variable")
        print("  3. Database is empty - no historical settlement epochs yet")
        print()
        print("For this backtest to work, we need:")
        print(f"  - At least {Phase3BacktestEngine.MIN_ANALOGS} analogs per prediction")
        print("  - Minimum 30-50 historical epochs for warmup")
        print("  - Closed settlement epochs with prior_bucket data")
        print()
        return None
    
    # Run backtest
    engine = Phase3BacktestEngine(db_path)
    
    try:
        print("Running backtest...")
        results = engine.run_backtest()
        
        print(f"Generated {len(results)} predictions")
        
        if not results:
            print("No predictions generated. Check data availability.")
            return None
        
        # Calculate summary
        print("Calculating summary statistics...")
        summary = engine.calculate_summary(results)
        
        # Print results
        print()
        print("=" * 80)
        print("BACKTEST RESULTS")
        print("=" * 80)
        print()
        
        # Overall metrics
        print("OVERALL METRICS")
        print("-" * 40)
        print(f"Total predictions:       {summary.total_predictions}")
        print(f"Valid analogs (≥{Phase3BacktestEngine.MIN_ANALOGS}):     {summary.total_valid_analogs}")
        print(f"  ({summary.valid_predictions_count} valid, {summary.invalid_predictions_count} invalid)")
        print()
        print(f"Directional accuracy:    {summary.directional_accuracy:.2%} ({summary.directional_correct}/{summary.total_predictions})")
        print(f"  Target: ≥{Phase3BacktestEngine.DIRECTIONAL_THRESHOLD:.0%} - {'✓ PASS' if summary.directional_accuracy >= Phase3BacktestEngine.DIRECTIONAL_THRESHOLD else '✗ FAIL'}")
        print()
        print(f"Reversion prediction:")
        print(f"  Accuracy:  {summary.reversion_accuracy:.2%} ({summary.reversion_correct}/{summary.reversion_total})")
        print(f"  Precision: {summary.reversion_precision:.2%}")
        print(f"  Recall:    {summary.reversion_recall:.2%}")
        print()
        print(f"Magnitude error:")
        print(f"  Average:  {summary.avg_magnitude_error:.2f} buckets")
        print(f"  Median:   {summary.median_magnitude_error:.2f} buckets")
        print(f"  Min:      {summary.min_magnitude_error:.2f} buckets")
        print(f"  Max:      {summary.max_magnitude_error:.2f} buckets")
        print()
        
        # Confidence calibration
        print("CONFIDENCE CALIBRATION")
        print("-" * 40)
        print(f"High confidence (≥{Phase3BacktestEngine.MIN_CONFIDENCE_THRESHOLD:.0%}):")
        print(f"  Count:    {summary.high_conf_total}")
        print(f"  Correct:  {summary.high_conf_correct}")
        print(f"  Accuracy: {summary.high_conf_accuracy:.2%}")
        print()
        print(f"Low confidence (<{Phase3BacktestEngine.MIN_CONFIDENCE_THRESHOLD:.0%}):")
        print(f"  Count:    {summary.low_conf_total}")
        print(f"  Correct:  {summary.low_conf_correct}")
        print(f"  Accuracy: {summary.low_conf_accuracy:.2%}")
        print()
        
        calibration_pass = summary.high_conf_accuracy > summary.low_conf_accuracy
        print(f"Calibration: {'✓ PASS' if calibration_pass else '✗ FAIL'}")
        print(f"  High conf accuracy > Low conf accuracy: {summary.high_conf_accuracy:.2%} > {summary.low_conf_accuracy:.2%}")
        print()
        
        # Signal validity
        print("SIGNAL VALIDITY")
        print("-" * 40)
        print(f"Predictions with ≥{Phase3BacktestEngine.MIN_ANALOGS} analogs: {summary.valid_predictions_count}")
        print(f"Predictions with <{Phase3BacktestEngine.MIN_ANALOGS} analogs: {summary.invalid_predictions_count}")
        print()
        
        # Station breakdown
        print("STATION BREAKDOWN")
        print("-" * 40)
        print(f"{'Station':<8} {'Total':>6} {'Correct':>8} {'Accuracy':>10} {'Rev Acc':>10}")
        print("-" * 46)
        
        for station in sorted(summary.station_stats.keys()):
            stats = summary.station_stats[station]
            rev_acc = stats.get('reversion_accuracy', 0)
            print(f"{station:<8} {stats['total']:>6} {stats['correct']:>8} "
                  f"{stats['accuracy']:>10.2%} {rev_acc:>10.2%}")
        
        print()
        
        # Market type breakdown
        print("MARKET TYPE BREAKDOWN")
        print("-" * 40)
        print(f"{'Market':<8} {'Total':>6} {'Correct':>8} {'Accuracy':>10} {'Rev Acc':>10}")
        print("-" * 46)
        
        for mt in sorted(summary.market_type_stats.keys()):
            stats = summary.market_type_stats[mt]
            rev_acc = stats.get('reversion_accuracy', 0)
            print(f"{mt:<8} {stats['total']:>6} {stats['correct']:>8} "
                  f"{stats['accuracy']:>10.2%} {rev_acc:>10.2%}")
        
        print()
        
        # Confidence band breakdown
        print("CONFIDENCE BAND BREAKDOWN")
        print("-" * 40)
        print(f"{'Band':<12} {'Total':>6} {'Correct':>8} {'Accuracy':>10} {'Avg Score':>10}")
        print("-" * 46)
        
        for band in sorted(summary.confidence_band_stats.keys()):
            stats = summary.confidence_band_stats[band]
            print(f"{band:<12} {stats['total']:>6} {stats['correct']:>8} "
                  f"{stats['accuracy']:>10.2%} {stats['avg_score']:>10.3f}")
        
        print()
        
        # Trajectory analysis
        print("TRAJECTORY ANALYSIS")
        print("-" * 40)
        print(f"Trajectory matches:     {summary.trajectory_matches}")
        print(f"Trajectory mismatches:  {summary.trajectory_mismatches}")
        print(f"No trajectory data:     {summary.no_trajectory_data}")
        print()
        
        # Date range
        print("DATE RANGE")
        print("-" * 40)
        print(f"Start: {summary.start_date}")
        print(f"End:   {summary.end_date}")
        print()
        
        # Final verdict
        print("=" * 80)
        print("FINAL VERDICT")
        print("=" * 80)
        print()
        
        directional_pass = summary.directional_accuracy >= Phase3BacktestEngine.DIRECTIONAL_THRESHOLD
        reversion_pass = summary.reversion_accuracy >= Phase3BacktestEngine.DIRECTIONAL_THRESHOLD  # Same threshold
        calibration_pass = summary.high_conf_accuracy > summary.low_conf_accuracy
        
        if directional_pass and calibration_pass:
            print("✓ SIGNAL CLEARS THRESHOLDS")
            print(f"  Directional accuracy: {summary.directional_accuracy:.2%} ≥ {Phase3BacktestEngine.DIRECTIONAL_THRESHOLD:.0%}")
            print(f"  Confidence calibration: High > Low ({summary.high_conf_accuracy:.2%} > {summary.low_conf_accuracy:.2%})")
            print()
            print("RECOMMENDATION: PROCEED WITH TRADING SYSTEM BUILD")
        else:
            print("✗ SIGNAL DOES NOT CLEAR THRESHOLDS")
            
            if not directional_pass:
                print(f"  Directional accuracy: {summary.directional_accuracy:.2%} < {Phase3BacktestEngine.DIRECTIONAL_THRESHOLD:.0%}")
            
            if not reversion_pass:
                print(f"  Reversion accuracy: {summary.reversion_accuracy:.2%} < {Phase3BacktestEngine.DIRECTIONAL_THRESHOLD:.0%}")
            
            if not calibration_pass:
                print(f"  Confidence calibration: High ({summary.high_conf_accuracy:.2%}) not > Low ({summary.low_conf_accuracy:.2%})")
            
            print()
            print("RECOMMENDATION: NEED MORE DATA OR FEATURE IMPROVEMENT")
        
        print()
        print("=" * 80)
        
        # Save detailed results
        output_path = "/tmp/p3_backtest_results_v2.json"
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        
        output = {
            "summary": {
                "total_predictions": summary.total_predictions,
                "total_valid_analogs": summary.total_valid_analogs,
                "directional_accuracy": summary.directional_accuracy,
                "reversion_accuracy": summary.reversion_accuracy,
                "confidence_calibration": summary.confidence_calibration,
                "station_count": len(summary.station_stats),
                "market_type_count": len(summary.market_type_stats),
                "band_count": len(summary.confidence_band_stats),
            },
            "detailed": {
                "directional": {
                    "accuracy": summary.directional_accuracy,
                    "correct": summary.directional_correct,
                    "total": summary.total_predictions,
                },
                "reversion": {
                    "accuracy": summary.reversion_accuracy,
                    "precision": summary.reversion_precision,
                    "recall": summary.reversion_recall,
                    "correct": summary.reversion_correct,
                    "total": summary.reversion_total,
                },
                "magnitude_error": {
                    "avg": summary.avg_magnitude_error,
                    "median": summary.median_magnitude_error,
                    "min": summary.min_magnitude_error,
                    "max": summary.max_magnitude_error,
                },
                "confidence": {
                    "high_accuracy": summary.high_conf_accuracy,
                    "low_accuracy": summary.low_conf_accuracy,
                    "calibration_passed": summary.confidence_calibration,
                },
                "stations": summary.station_stats,
                "market_types": summary.market_type_stats,
                "confidence_bands": summary.confidence_band_stats,
            },
            "thresholds": {
                "directional": Phase3BacktestEngine.DIRECTIONAL_THRESHOLD,
                "confidence": Phase3BacktestEngine.CALIBRATION_THRESHOLD,
                "min_analogs": Phase3BacktestEngine.MIN_ANALOGS,
            },
            "date_range": {
                "start": summary.start_date,
                "end": summary.end_date,
            },
            "recommendation": "PROCEED" if (directional_pass and calibration_pass) else "REVIEW",
        }
        
        with open(output_path, 'w') as f:
            json.dump(output, f, indent=2)
        
        print(f"Detailed results saved to: {output_path}")
        
        return output
        
    finally:
        engine.close()


if __name__ == "__main__":
    result = main()
