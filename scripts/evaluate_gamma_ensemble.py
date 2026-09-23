#!/usr/bin/env python3
"""
evaluate_gamma_ensemble.py — Evaluation pipeline for Gamma-82 Ensemble Fusion

Comprehensive evaluation pipeline for the integration of Gamma signals (G1-G6) 
with the 82-member ensemble system.
"""

import argparse
import sqlite3
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import Dict, List, Tuple, Optional
import json
import logging
from pathlib import Path
import os

from core.gamma_ensemble_fusion import GammaEnsembleFusion, FusionResult
from core.signals.eighty_two_member_ensemble_signal import EightyTwoMemberEnsembleSignal
from core.signals.gamma_signals import get_gamma_signals

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


class GammaEnsembleEvaluator:
    """
    Comprehensive evaluator for Gamma-82 ensemble fusion system.
    """
    
    def __init__(self, db_path: str, start_date: str = None, end_date: str = None):
        """
        Initialize the evaluator.
        
        Args:
            db_path: Path to the METAR database
            start_date: Start date for evaluation (YYYY-MM-DD)
            end_date: End date for evaluation (YYYY-MM-DD)
        """
        self.db_path = db_path
        self.start_date = start_date
        self.end_date = end_date
        self.conn = sqlite3.connect(db_path)
        
        # Initialize fusion components
        self.fusion = GammaEnsembleFusion(db_path=db_path)
        self.ensemble_signal = EightyTwoMemberEnsembleSignal(db_path=db_path)
        self.gamma_signals = get_gamma_signals(db_path=db_path)
        
        # Prepare results storage
        self.results = []
        self.daily_predictions = []
        
    def load_station_data(self, station: str, date_range: Tuple[str, str]) -> List[dict]:
        """
        Load historical station data for evaluation.
        
        Args:
            station: Station identifier (e.g., 'KJFK')
            date_range: Tuple of (start_date, end_date)
            
        Returns:
            List of daily data dictionaries
        """
        start_dt, end_dt = date_range
        
        # Query to get daily METAR data
        query = """
        SELECT 
            date_utc as date,
            MAX(temp_f) as high,
            MIN(temp_f) as low,
            AVG(temp_f) as temp,
            AVG(dewpoint_f) as dewpoint,
            AVG(pressure_mb) as pressure,
            AVG(wind_direction_deg) as wind_dir,
            AVG(wind_speed_kt) as wind_speed,
            AVG(visibility_mi) as visibility
        FROM metar_observations
        WHERE station = ? 
          AND date_utc BETWEEN ? AND ?
        GROUP BY date_utc
        ORDER BY date_utc ASC
        """
        
        cursor = self.conn.cursor()
        cursor.execute(query, (station, start_dt, end_dt))
        rows = cursor.fetchall()
        
        # Convert to list of dictionaries
        days = []
        for row in rows:
            day_dict = {
                'date': row[0],
                'high': row[1],
                'low': row[2], 
                'temp': row[3],
                'dewpoint': row[4],
                'pressure': row[5],
                'wind_dir': row[6],
                'wind_speed': row[7],
                'visibility': row[8],
                'station': station
            }
            days.append(day_dict)
        
        return days
    
    def load_settlement_data(self, station: str, date_range: Tuple[str, str]) -> Dict[str, str]:
        """
        Load settlement data to determine actual outcomes.
        
        Args:
            station: Station identifier
            date_range: Tuple of (start_date, end_date)
            
        Returns:
            Dictionary mapping date to actual outcome ('up', 'down', 'flat')
        """
        start_dt, end_dt = date_range
        
        query = """
        SELECT 
            local_trading_date,
            settlement_bucket,
            prior_settlement_bucket
        FROM settlement_epochs
        WHERE station = ? 
          AND market_type = 'HIGH'
          AND epoch_status = 'closed'
          AND local_trading_date BETWEEN ? AND ?
        """
        
        cursor = self.conn.cursor()
        cursor.execute(query, (station, start_dt, end_dt))
        rows = cursor.fetchall()
        
        settlements = {}
        for row in rows:
            date, current_bucket, prior_bucket = row
            if prior_bucket is not None:
                if current_bucket > prior_bucket:
                    settlements[date] = 'up'
                elif current_bucket < prior_bucket:
                    settlements[date] = 'down'
                else:
                    settlements[date] = 'flat'
        
        return settlements
    
    def evaluate_day(self, station: str, date_idx: int, days: List[dict], settlements: Dict[str, str]) -> Optional[Dict]:
        """
        Evaluate all systems for a single day.
        
        Args:
            station: Station identifier
            date_idx: Index in days array
            days: List of daily data
            settlements: Settlement outcomes dictionary
            
        Returns:
            Dictionary with evaluation results for the day
        """
        target_date = days[date_idx]['date']
        
        # Skip if no settlement data for this date
        actual = settlements.get(target_date)
        if actual is None or actual == 'flat':
            return None
        
        try:
            # Get fusion result
            fusion_result = self.fusion.fuse(date_idx, days)
            
            # Get individual ensemble result
            ensemble_dir, ensemble_conf = self.ensemble_signal.evaluate(date_idx, days)
            
            # Get individual gamma signal results
            gamma_results = {}
            for i, (name, signal) in enumerate(self.fusion.gamma_signal_map.items()):
                try:
                    gamma_dir, gamma_conf = signal.evaluate(date_idx, days)
                    gamma_results[f'g{i+1}_direction'] = gamma_dir
                    gamma_results[f'g{i+1}_confidence'] = gamma_conf
                except Exception as e:
                    logger.warning(f"Gamma signal {name} failed for {station} on {target_date}: {e}")
                    gamma_results[f'g{i+1}_direction'] = None
                    gamma_results[f'g{i+1}_confidence'] = 0.0
            
            return {
                'station': station,
                'date': target_date,
                'actual': actual,
                'fusion_direction': fusion_result.direction,
                'fusion_confidence': fusion_result.confidence,
                'ensemble_direction': ensemble_dir,
                'ensemble_confidence': ensemble_conf,
                'gamma_direction': fusion_result.gamma_direction,
                'gamma_confidence': fusion_result.gamma_confidence,
                'agreement_score': fusion_result.agreement_score,
                'adjusted_confidence': fusion_result.adjusted_confidence,
                **gamma_results
            }
            
        except Exception as e:
            logger.warning(f"Evaluation failed for {station} on {target_date}: {e}")
            return None
    
    def run_evaluation(self, stations: List[str], date_range: Tuple[str, str]) -> Dict:
        """
        Run comprehensive evaluation across multiple stations and dates.
        
        Args:
            stations: List of station identifiers
            date_range: Tuple of (start_date, end_date)
            
        Returns:
            Dictionary with comprehensive evaluation results
        """
        logger.info(f"Starting evaluation for {len(stations)} stations from {date_range[0]} to {date_range[1]}")
        
        all_results = []
        
        for station in stations:
            logger.info(f"Evaluating station: {station}")
            
            # Load data for station
            days = self.load_station_data(station, date_range)
            settlements = self.load_settlement_data(station, date_range)
            
            if len(days) < 5:  # Need sufficient data
                logger.warning(f"Insufficient data for {station}, skipping")
                continue
            
            # Evaluate each day
            station_results = []
            for i in range(4, len(days)):  # Start from index 4 to ensure sufficient lookback
                result = self.evaluate_day(station, i, days, settlements)
                if result:
                    station_results.append(result)
            
            all_results.extend(station_results)
            logger.info(f"Processed {len(station_results)} days for {station}")
        
        # Calculate metrics
        metrics = self.calculate_metrics(all_results)
        
        # Store results
        self.results = all_results
        
        evaluation_summary = {
            'evaluation_parameters': {
                'stations': stations,
                'date_range': date_range,
                'total_predictions': len(all_results),
                'evaluation_timestamp': datetime.now().isoformat()
            },
            'metrics': metrics,
            'results': all_results
        }
        
        return evaluation_summary
    
    def calculate_metrics(self, results: List[Dict]) -> Dict:
        """
        Calculate comprehensive metrics from evaluation results.
        
        Args:
            results: List of daily evaluation results
            
        Returns:
            Dictionary with calculated metrics
        """
        if not results:
            return {}
        
        # Convert to DataFrame for easier analysis
        df = pd.DataFrame(results)
        
        # Overall accuracy metrics
        total_predictions = len(df)
        
        # Fusion system metrics
        fusion_valid = df.dropna(subset=['fusion_direction'])
        fusion_correct = fusion_valid[fusion_valid['fusion_direction'] == fusion_valid['actual']]
        fusion_accuracy = len(fusion_correct) / len(fusion_valid) if len(fusion_valid) > 0 else 0
        
        # Ensemble-only metrics
        ensemble_valid = df.dropna(subset=['ensemble_direction'])
        ensemble_correct = ensemble_valid[ensemble_valid['ensemble_direction'] == ensemble_valid['actual']]
        ensemble_accuracy = len(ensemble_correct) / len(ensemble_valid) if len(ensemble_valid) > 0 else 0
        
        # Gamma-only metrics (when active)
        gamma_valid = df.dropna(subset=['gamma_direction'])
        gamma_correct = gamma_valid[gamma_valid['gamma_direction'] == gamma_valid['actual']]
        gamma_accuracy = len(gamma_correct) / len(gamma_valid) if len(gamma_valid) > 0 else 0
        
        # Confidence-weighted accuracy
        fusion_weighted_correct = sum(
            df.loc[(df['fusion_direction'] == df['actual']) & df['fusion_direction'].notna(), 'fusion_confidence']
        )
        fusion_weighted_total = sum(df['fusion_confidence'].fillna(0))
        fusion_weighted_accuracy = fusion_weighted_correct / fusion_weighted_total if fusion_weighted_total > 0 else 0
        
        # Agreement analysis
        high_agreement = df[df['agreement_score'] >= 0.7]
        low_agreement = df[df['agreement_score'] < 0.3]
        
        high_agreement_accuracy = 0
        low_agreement_accuracy = 0
        if len(high_agreement) > 0:
            high_correct = high_agreement[high_agreement['fusion_direction'] == high_agreement['actual']]
            high_agreement_accuracy = len(high_correct) / len(high_agreement)
        if len(low_agreement) > 0:
            low_correct = low_agreement[low_agreement['fusion_direction'] == low_agreement['actual']]
            low_agreement_accuracy = len(low_correct) / len(low_agreement)
        
        # Per-signal analysis for Gamma signals
        gamma_signal_metrics = {}
        for i in range(1, 7):
            col_dir = f'g{i}_direction'
            col_conf = f'g{i}_confidence'
            
            signal_valid = df.dropna(subset=[col_dir])
            if len(signal_valid) > 0:
                signal_correct = signal_valid[signal_valid[col_dir] == signal_valid['actual']]
                signal_accuracy = len(signal_correct) / len(signal_valid)
                avg_confidence = signal_valid[col_conf].mean()
                
                gamma_signal_metrics[f'g{i}'] = {
                    'accuracy': signal_accuracy,
                    'avg_confidence': avg_confidence,
                    'predictions': len(signal_valid),
                    'correct': len(signal_correct)
                }
        
        return {
            'overall': {
                'total_predictions': total_predictions,
                'fusion_accuracy': fusion_accuracy,
                'ensemble_accuracy': ensemble_accuracy,
                'gamma_accuracy': gamma_accuracy,
                'fusion_weighted_accuracy': fusion_weighted_accuracy,
                'fusion_predictions': len(fusion_valid),
                'ensemble_predictions': len(ensemble_valid),
                'gamma_predictions': len(gamma_valid)
            },
            'agreement_analysis': {
                'high_agreement_count': len(high_agreement),
                'low_agreement_count': len(low_agreement),
                'high_agreement_accuracy': high_agreement_accuracy,
                'low_agreement_accuracy': low_agreement_accuracy,
                'avg_agreement_score': df['agreement_score'].mean() if 'agreement_score' in df.columns else 0
            },
            'gamma_signals': gamma_signal_metrics,
            'summary': {
                'improvement_over_ensemble': fusion_accuracy - ensemble_accuracy,
                'significant_improvement': (fusion_accuracy - ensemble_accuracy) > 0.01,  # >1% improvement
                'sample_size': len(fusion_valid)
            }
        }
    
    def save_results(self, output_path: str):
        """
        Save evaluation results to JSON file.
        
        Args:
            output_path: Path to save results JSON file
        """
        output_dir = Path(output_path).parent
        output_dir.mkdir(parents=True, exist_ok=True)
        
        with open(output_path, 'w') as f:
            json.dump({
                'timestamp': datetime.now().isoformat(),
                'results': self.results
            }, f, indent=2, default=str)
        
        logger.info(f"Results saved to {output_path}")
    
    def print_summary(self, evaluation_summary: Dict):
        """
        Print a formatted summary of evaluation results.
        
        Args:
            evaluation_summary: Dictionary from run_evaluation
        """
        metrics = evaluation_summary['metrics']
        
        print("\n" + "="*60)
        print("GAMMA-82 ENSEMBLE FUSION EVALUATION SUMMARY")
        print("="*60)
        
        print(f"\nEvaluation Period: {evaluation_summary['evaluation_parameters']['date_range'][0]} to {evaluation_summary['evaluation_parameters']['date_range'][1]}")
        print(f"Stations Evaluated: {len(evaluation_summary['evaluation_parameters']['stations'])}")
        print(f"Total Predictions: {metrics['overall']['total_predictions']}")
        
        print(f"\nACCURACY METRICS:")
        print(f"  Fusion System Accuracy:     {metrics['overall']['fusion_accuracy']:.3f} ({metrics['overall']['fusion_predictions']} predictions)")
        print(f"  Ensemble-Only Accuracy:     {metrics['overall']['ensemble_accuracy']:.3f} ({metrics['overall']['ensemble_predictions']} predictions)")
        print(f"  Gamma-Only Accuracy:        {metrics['overall']['gamma_accuracy']:.3f} ({metrics['overall']['gamma_predictions']} predictions)")
        print(f"  Fusion Weighted Accuracy:   {metrics['overall']['fusion_weighted_accuracy']:.3f}")
        
        improvement = metrics['summary']['improvement_over_ensemble']
        print(f"\nIMPROVEMENT OVER BASELINE:   {improvement:+.3f} ({'+' if improvement > 0 else ''}{improvement*100:+.2f}%)")
        
        print(f"\nAGREEMENT ANALYSIS:")
        print(f"  High Agreement Accuracy:    {metrics['agreement_analysis']['high_agreement_accuracy']:.3f} ({metrics['agreement_analysis']['high_agreement_count']} days)")
        print(f"  Low Agreement Accuracy:     {metrics['agreement_analysis']['low_agreement_accuracy']:.3f} ({metrics['agreement_analysis']['low_agreement_count']} days)")
        print(f"  Avg Agreement Score:        {metrics['agreement_analysis']['avg_agreement_score']:.3f}")
        
        print(f"\nGAMMA SIGNAL PERFORMANCE:")
        for i in range(1, 7):
            g_key = f'g{i}'
            if g_key in metrics['gamma_signals']:
                g_metrics = metrics['gamma_signals'][g_key]
                print(f"  {g_key.upper()}: {g_metrics['accuracy']:.3f} accuracy, "
                      f"{g_metrics['avg_confidence']:.3f} avg confidence, "
                      f"{g_metrics['predictions']} predictions")
        
        print("="*60)


def main():
    parser = argparse.ArgumentParser(description='Evaluate Gamma-82 Ensemble Fusion System')
    parser.add_argument('--db-path', type=str, required=True, help='Path to METAR database')
    parser.add_argument('--start-date', type=str, required=True, help='Start date (YYYY-MM-DD)')
    parser.add_argument('--end-date', type=str, required=True, help='End date (YYYY-MM-DD)')
    parser.add_argument('--stations', nargs='+', default=[
        'KJFK', 'KLAX', 'KORD', 'KATL', 'KSEA', 'KDEN', 'KMIA', 'KPHX', 'KDFW', 'KCLT'
    ], help='List of station codes to evaluate')
    parser.add_argument('--output', type=str, default='./gamma_ensemble_evaluation.json', 
                       help='Output path for evaluation results')
    
    args = parser.parse_args()
    
    # Validate dates
    try:
        start_dt = datetime.strptime(args.start_date, '%Y-%m-%d')
        end_dt = datetime.strptime(args.end_date, '%Y-%m-%d')
        if start_dt > end_dt:
            raise ValueError("Start date must be before end date")
    except ValueError as e:
        print(f"Date parsing error: {e}")
        return
    
    # Initialize evaluator
    evaluator = GammaEnsembleEvaluator(
        db_path=args.db_path,
        start_date=args.start_date,
        end_date=args.end_date
    )
    
    # Run evaluation
    evaluation_summary = evaluator.run_evaluation(
        stations=args.stations,
        date_range=(args.start_date, args.end_date)
    )
    
    # Print summary
    evaluator.print_summary(evaluation_summary)
    
    # Save results
    evaluator.save_results(args.output)
    
    print(f"\nDetailed results saved to: {args.output}")


if __name__ == "__main__":
    main()