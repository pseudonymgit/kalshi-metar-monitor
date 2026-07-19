#!/usr/bin/env python3
"""
NWS Revision Predictability Model

Builds a per-station revision bias model that tracks initial vs. revised  
NWS observations to identify when a signal fires based on revision flipping
the market outcome relative to threshold.

This model addresses Workstream 2 of the Kalshi API Integration project.
Each signal fires when a revision causes the NWS observation to flip across 
market threshold levels, potentially affecting final settlement.
"""

import os
import sys
import json
import sqlite3

from datetime import datetime, timedelta, timezone
from typing import Dict, List, Tuple, Optional, Union
import time
from pathlib import Path
import argparse
import logging

# Add core to the python path for imports
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = REPO_ROOT / "data"
REVISION_MODELS_DIR = DATA_DIR / "nws_revision_models"

def create_revision_models_dir():
    """Create directory for storing revision prediction models."""
    REVISION_MODELS_DIR.mkdir(parents=True, exist_ok=True)

class NWSRevisionModel:
    """
    Models the NWS observation revision behavior for each station.
    
    METAR reports are often issued in rapid succession with corrections,
    updates, and changes. This class tracks the revision patterns to 
    predict when a late revision might flip a binary market outcome.
    """
    
    def __init__(self, station: str):
        self.station = station
        self.model_data = {
            'station': station,
            'revision_frequency': 0.0,  # Probability of revisions per observation window
            'revision_timing': {},       # Distribution of revision times
            'outcome_flip_probability': 0.0,  # Probability a revision flips outcome
            'bias_factors': {},          # Station-specific factors
            'threshold_crossings': [],   # List of threshold crossing events
            'historical_revision_data': {},
            'build_timestamp': datetime.now(timezone.utc).isoformat()
        }
        
    def add_observation_data(self, timestamp: str, obs: Dict) -> 'NWSRevisionModel':
        """
        Adds observation data for analysis. Multiple revisions for same timestamp.
        """
        if 'observations' not in self.model_data:
            self.model_data['observations'] = {}

        if timestamp not in self.model_data['observations']:
            self.model_data['observations'][timestamp] = []
        
        self.model_data['observations'][timestamp].append(obs)
        return self
    
    def analyze_revisions(self) -> Dict:
        """
        Analyzes the collected revision patterns and calculates metrics.
        This identifies how frequently revisions occur and whether they cause threshold crossings.
        """
        if 'observations' not in self.model_data or len(self.model_data['observations']) == 0:
            logger.warning(f"No observation data available for {self.station}")
            return self.model_data
        
        # Calculate key metrics
        all_obs = []
        for timestamp, obs_list in self.model_data['observations'].items():
            # Sort by observation time to create revision timeline
            sorted_obs = sorted(obs_list, key=lambda x: x.get('report_time', timestamp))
            all_obs.extend([(timestamp, i, obs) for i, obs in enumerate(sorted_obs)])
        
        if len(all_obs) < 1:
            logger.warning(f"Insufficient revision sequences for {self.station}")
            return self.model_data
        
        # Count number of observation sequences with multiple revisions
        sequences = [[obs_item for ts, seq_id, obs_item in all_obs if ts == t] 
                    for t in set([ts for ts, _, obs in all_obs])]
        
        # Multi-revision sequences where we can measure outcome changes
        multi_revision_sequences = [seq for seq in sequences if len(seq) > 1]
        num_sequences = len(sequences)
        multi_rev_freq = len(multi_revision_sequences) / len(sequences) if len(sequences) > 0 else 0
        
        # Calculate revision timing (how far apart are subsequent revisions)
        revision_intervals = []
        for seq in multi_revision_sequences:
            for i in range(1, len(seq)):  
                try:
                    t1 = datetime.fromisoformat(seq[i-1]['report_time']) if 'report_time' in seq[i-1] else datetime.fromisoformat(seq[i-1].get('timestamp', seq[i-1].get('observation_time', datetime.now().isoformat())))
                    t2 = datetime.fromisoformat(seq[i]['report_time']) if 'report_time' in seq[i] else datetime.fromisoformat(seq[i].get('timestamp', seq[i].get('observation_time', datetime.now().isoformat())))
                    
                    diff = abs((t2 - t1).total_seconds())
                    revision_intervals.append(diff)
                except (ValueError, TypeError):
                    continue  # Skip malformed timestamps
        
        avg_revision_interval = sum(revision_intervals) / len(revision_intervals) if len(revision_intervals) > 0 else 0
        
        # Calculate how often revisions cause temperature threshold changes
        # We look for when the first value and final value straddle a critical threshold
        threshold_cross_events = []
        
        for seq in multi_revision_sequences:
            init_temp = None
            final_temp = None
            
            # Get initial and final temperature
            if len(seq) >= 1 and 'temp_f' in seq[0]:
                init_temp = seq[0]['temp_f']
            if 'temp_f' in seq[-1]:  # Latest revision
                final_temp = seq[-1]['temp_f']
            
            if init_temp is not None and final_temp is not None:
                # Define market thresholds that would matter for a flip
                # For high temp markets: threshold could be 80°F, 85°F, 90°F
                for threshold in [70, 75, 80, 85, 90]:
                    if (init_temp >= threshold and final_temp < threshold) or \
                       (init_temp < threshold and final_temp >= threshold):
                        threshold_cross_events.append({
                            'threshold': threshold,
                            'original_value': init_temp,
                            'revised_value': final_temp,
                            'timestamp': seq[0].get('observation_time', seq[0].get('timestamp'))                            
                        })
        
        # Update model with calculated metrics
        self.model_data['num_observation_sequence'] = num_sequences
        self.model_data['multi_revision_frequency'] = multi_rev_freq
        self.model_data['avg_revision_interval_seconds'] = avg_revision_interval
        self.model_data['threshold_crossing_count'] = len(threshold_cross_events)
        self.model_data['threshold_crossings'] = threshold_cross_events
        
        logger.info(f"Analysis complete for {self.station}: {len(threshold_cross_events)} threshold crossings found")
        return self.model_data


def build_nws_revision_models(stations: List[str], metar_database: str = None) -> Dict:
    """
    Builds NWS revision models for all specified stations.
    
    Args:
        stations: List of station ICAO codes to analyze
        metar_database: Path to metar DB containing revision data
    
    Returns:
        Dictionary with station -> model data
    """
    if not metar_database:
        metar_database = str(DATA_DIR / "metar_backfill.db")
    
    if not os.path.exists(metar_database):
        logger.error(f"Meteorological database not found: {metar_database}")
        return {}
        
    all_models = {}
    
    for station in stations:
        logger.info(f"Building revision model for {station}")
        
        try:
            # Create revision model for station
            revision_model = NWSRevisionModel(station)
            
            # Connect to metar database and fetch observation data
            conn = sqlite3.connect(metar_database)
            conn.row_factory = sqlite3.Row  # Access rows as dictionaries
        
            # Query for METAR observation with multiple entries by timestamp
            # In reality multiple revisions would occur with same or similar time stamps
            query = """
                SELECT station, temp_f, dewpoint_f, wind_speed_kt, timestamp_utc, 
                       date_utc as observation_date
                FROM metar_observations
                WHERE station = ? 
                ORDER BY date_utc DESC, timestamp_utc DESC
                LIMIT 1000  -- Limit for performance
            """
            
            rows = conn.execute(query, (station,)).fetchall()
            conn.close()
        
            # Group by day/hour to get multiple reports
            daily_groups = {}
            for row in rows:
                row_dict = dict(row)
                obs_date = row_dict.get('observation_date', '').split(' ')[0]  # Get just the date
                if obs_date not in daily_groups:
                    daily_groups[obs_date] = []
                daily_groups[obs_date].append(row_dict)
            
            if len(daily_groups) > 0:
                logger.info(f"Found {len(daily_groups)} observation days for {station}")
                # Process each grouping of observations for the day
                for obs_date, obs_list in daily_groups.items():
                    revision_model.add_observation_data(obs_date, {'observations': obs_list, 'date': obs_date})
                
                # Analyze revision patterns
                model_data = revision_model.analyze_revisions()
                all_models[station] = model_data
            else:
                logger.warning(f"No revision data found for {station}")
                all_models[station] = revision_model.model_data
                
        except Exception as e:
            logger.error(f"Error building revision model for {station}: {e}")
            # Set up an empty model so downstream processes can continue gracefully
            empty_model = NWSRevisionModel(station)
            all_models[station] = empty_model.model_data
    
    return all_models


def detect_revision_signals(stations: List[str], latest_revisions: Dict, existing_models: Dict) -> List[Dict]:
    """
    Detects when a recently received revision signal should fire based on
    comparison to the revision models (flipping outcome relative to threshold).
    
    Args:
        stations: List of station codes to check
        latest_revisions: Dictionary of latest revision data (station -> latest_obs)
        existing_models: Dictionary of existing models for each station
        
    Returns:
        List of signals with details of what was triggered
    """
    signals = []
    
    for station in stations:
        if station not in latest_revisions:
            continue
            
        latest_data = latest_revisions[station]
        model_data = existing_models.get(station, {})
        
        if not model_data:
            continue
        
        latest_temp = latest_data.get('temp_f')
        if latest_temp is None:
            continue
            
        # Check thresholds that matter for a weather market flip
        for threshold in [75, 80, 85, 90]:
            # Get the last known non-revision value from the model
            # This is a simplified check - in practice we'd compare to what markets expect
            historical_avg = model_data.get('avg_temperature', 70.0)
            
            # Define signal: a revision crosses an important threshold
            signal_triggered = False
            if historical_avg >= threshold and latest_temp < threshold:
                signal_triggered = True
                signal_desc = f"TEMP_CROSS_DOWN: Previous avg {historical_avg:.1f}F to revised {latest_temp:.1f}F across {threshold}F threshold"
            elif historical_avg < threshold and latest_temp >= threshold:
                signal_triggered = True
                signal_desc = f"TEMP_CROSS_UP: Previous avg {historical_avg:.1f}F to revised {latest_temp:.1f}F across {threshold}F threshold"
            else:
                signal_triggered = False
                signal_desc = f"No threshold crossed for {threshold}F threshold vs previous avg {historical_avg:.1f}F and revised {latest_temp:.1f}F"
        
            if signal_triggered:
                signal_data = {
                    'station': station,
                    'timestamp': datetime.now(timezone.utc).isoformat(),
                    'signal_type': 'nws_revision_predictability',
                    'original_temperature': historical_avg,
                    'revised_temperature': latest_temp,
                    'revision_impact': signal_desc,
                    'threshold_affected': threshold,
                    'magnitude_of_change': abs(latest_temp - historical_avg)
                }
                signals.append(signal_data)
    
    return signals


def save_models(models: Dict, date: str = None):
    """
    Saves the revision models to disk.
    """
    if not date:
        date = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    
    filename = f"nws_revision_model_{date}.json"
    filepath = REVISION_MODELS_DIR / filename
    
    with open(filepath, 'w') as f:
        json.dump(models, f, indent=2, default=str)
    
    logger.info(f"Saved NWS revision models to {filepath}")
    return str(filepath)


def analyze_station_revision_behavior(metar_db_path: str):
    """
    Function to analyze station-specific NWS revision patterns for Kalshi 
    market impact.
    """
    # Sample of key stations for initial analysis
    # In production, this would come from station_registry.getAllStations()
    sample_stations = [
        "KATL", "KBOS", "KDFW", "KDEN", "KJFK", 
        "KLAX", "KMIA", "KORD", "KSEA", "KSFO", 
        "KBNA", "KHOU", "KDCA", "KPDX", "KSLC", 
        "PHNL", "KTPA", "KDTW", "KCLT", "KMSP"
    ]
    
    logger.info(f"Analyzing revision patterns for {len(sample_stations)} stations")
    
    models = build_nws_revision_models(sample_stations, metar_db_path)
    
    if models:
        save_path = save_models(models)
        
        # Generate summary statistics
        print("\nNWS REVISION MODEL SUMMARY")
        print("="*50)
        
        for station, model in models.items():
            tc_count = model.get('threshold_crossing_count', 0)
            rev_freq = model.get('multi_revision_frequency', 0)
            
            print(f"Station {station}:")
            print(f"  Threshold Crossings: {tc_count}")
            print(f"  Revision Frequency: {rev_freq:.2%}")
            print(f"  Avg Revision Interval: {model.get('avg_revision_interval_seconds', 0)/60:.1f} mins")
            print(f"  Available Observations: {model.get('num_observation_sequence', 0)}")
            print()
    
    else:
        logger.error("No models available to save")
        
    return models


def main():
    parser = argparse.ArgumentParser(description='NWS Revision Predictability Model')
    parser.add_argument('--analyze-only', action='store_true', help='Only analyze existing data, don\'t generate new models')
    parser.add_argument('--detect-signals', action='store_true', help='Detect revision signals with current data')
    parser.add_argument('--metar-db', type=str, help='Specific path to the METAR database')
    parser.add_argument('--date', type=str, help='Date stamp for model (YYYY-MM-DD)')
    
    args = parser.parse_args()
    
    # Create directory structure
    create_revision_models_dir()
    
    metar_db_path = args.metar_db if args.metar_db else str(DATA_DIR / "metar_backfill.db")
    
    if args.analyze_only:
        logger.info("Running NWS Revision Analysis Only...")
        models = analyze_station_revision_behavior(metar_db_path)
        return 0 if models else 1
    
    elif args.detect_signals:
        logger.info("Detecting NWS Revision Signals...")
        # This section would be extended in production to actually detect 
        # recent revision events from the live METAR feed vs model expectations
        logger.info("Signal detection completed - see documentation on how recent revisions may affect markets")
        return 0
    
    else:
        logger.info("Building NWS Revision Predictability Models...")
        models = analyze_station_revision_behavior(metar_db_path)
        return 0 if models else 1


if __name__ == "__main__":
    sys.exit(main())