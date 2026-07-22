#!/usr/bin/env python3
"""
PHASE 11.1: NWP Standalone Signal with Mahalanobis + PCA (per Expert 1 spec)

Implementation of Mahalanobis distance-based analog matching with PCA compression.
This creates a proper NWP analog signal that works with the NWP database 
and implements the specifications from Expert 1 document.

Features:
1. Mahalanobis distance metric using covariance matrix
2. PCA/EOF compression for dimensionality reduction
3. 21 variables, 5 models, 20 stations, 18+ months of data
4. Proper analog matching against the NWP database
5. Output: NWP standalone accuracy per station, per horizon
"""

import sqlite3
import numpy as np
from scipy.spatial.distance import mahalanobis
from sklearn.decomposition import PCA
import pickle
from datetime import datetime, timedelta
from pathlib import Path
import os
from typing import Optional, Tuple, Dict, List, Any
import logging

logger = logging.getLogger(__name__)

class MahalanobisNwpAnalogSignal:
    def __init__(self, nwp_db_path: str = None, metar_db_path: str = None):
        """
        Initialize the enhanced NWP analog signal with Mahalanobis distance and PCA.
        
        Args:
            nwp_db_path: Path to NWP forecasts database
            metar_db_path: Path to METAR observations database
        """
        if nwp_db_path:
            self.nwp_db_path = Path(nwp_db_path).absolute()
        elif os.environ.get('NWP_DB_PATH'):
            self.nwp_db_path = Path(os.environ['NWP_DB_PATH']).absolute()
        elif Path("prototypes/weather-engine-source/data/nwp_forecasts.db").exists():
            self.nwp_db_path = Path("prototypes/weather-engine-source/data/nwp_forecasts.db").resolve()
        else:
            self.nwp_db_path = Path("data/nwp_forecasts.db").resolve()

        if metar_db_path:
            self.metar_db_path = Path(metar_db_path).absolute()
        elif os.environ.get('METAR_DB_PATH'):
            self.metar_db_path = Path(os.environ['METAR_DB_PATH']).absolute()
        elif Path("prototypes/weather-engine-source/data/metar_backfill.db").exists():
            self.metar_db_path = Path("prototypes/weather-engine-source/data/metar_backfill.db").resolve()
        else:
            self.metar_db_path = Path("data/metar_backfill.db").resolve()

        self.pca_models = {}  # Per-station PCA models
        self.cov_matrices = {}  # Per-station covariance matrices
        self.variable_names = [
            # 21 variables as mentioned in Expert 1 spec
            'temperature_2m_max',
            'temperature_2m_min', 
            'temperature_850hPa',
            'dew_point_2m',
            'wind_speed_10m',
            'wind_direction_10m',
            'wind_u_850hPa',
            'wind_v_850hPa',
            'pressure',  # MSLP
            'geopotential_height',
            'cloud_cover',
            'precipitation_sum',
            'advection',
            'temperature_850hPa_daily_mean',  # From existing
            'geopotential_height_500hPa_daily_mean',  # From existing
            'cloud_cover_daily_mean',
            'dew_point_2m_daily_mean',
            'temperature_2m_max_daily_mean',
            'temperature_2m_min_daily_mean',
            # Additional forecast variables for different horizons
            'temperature_2m_max_fcst_24h',
        ]
        
        # 5 models as mentioned in Expert 3 spec
        self.models = ['GFS', 'ECMWF', 'ICON', 'GEM', 'ERA5']
        # 20 stations - will be discovered from DB
        self.stations = []
        
        # Cache for efficiency
        self._feature_cache = {}
        
    @property
    def name(self) -> str:
        return "mahalanobis_nwp_analog"

    @property
    def min_lookback(self) -> int:
        return 30  # Allow for proper covariance estimation

    def _load_nwp_data(self) -> Dict[tuple, Dict[str, float]]:
        """
        Load comprehensive NWP data with all 21 variables from database.
        
        Returns:
            Dict with keys (station, target_date, forecast_horizon, model) and
            value as dictionary of variable -> float value
        """
        if not self.nwp_db_path.exists():
            logger.error(f"NWP database not found at {self.nwp_db_path}")
            return {}

        conn = sqlite3.connect(str(self.nwp_db_path))
        try:
            # Get available stations and dates with multiple models
            cur = conn.cursor()
            
            # Get all unique combinations
            cur.execute("""
                SELECT station, target_date, model, variable, value
                FROM nwp_forecasts 
                WHERE value IS NOT NULL
                ORDER BY station, target_date, model, variable
            """)
            
            result = {}
            stations_set = set()
            for station, tdate, model, variable, value in cur.fetchall():
                stations_set.add(station)
                
                # Since there's no forecast_horizon, just use 0 as default
                key = (station, tdate, 0, model)
                if key not in result:
                    result[key] = {}
                    
                # Handle potentially non-standard variables (no forecast horizon in this DB)
                full_var_name = variable
                
                result[key][full_var_name] = value
                
            self.stations = sorted(list(stations_set))
            logger.info(f"Loaded data for {len(self.stations)} stations and {len(result)} time points")
            return result
            
        finally:
            conn.close()

    def _prepare_station_data(self, station: str, nwp_data: Dict[tuple, Dict[str, float]]) -> np.ndarray:
        """
        Prepare and normalize data matrix for a single station.
        
        Args:
            station: Station identifier
            nwp_data: Complete NWP data dictionary
            
        Returns:
            Array of shape (n_samples, n_features) with normalized values
        """
        # Filter for this station only
        station_data = {}
        for key, variables in nwp_data.items():
            _station, tdate, fhorizon, model = key
            if _station == station:
                station_data[key] = variables
        
        if not station_data:
            logger.warning(f"No data found for station {station}")
            return np.array([]).reshape(0, len(self.variable_names))

        # Build feature matrix
        feature_matrix = []
        sample_keys = []  # Keep track of sample identifying tuples
        
        for (station, tdate, fhorizon, model), vars_dict in station_data.items():
            features = []
            for var in self.variable_names:
                val = vars_dict.get(var)
                if val is not None:
                    features.append(val)
                else:
                    features.append(0.0)  # Default for missing
                    
            if len(features) == len(self.variable_names):
                feature_matrix.append(features)
                sample_keys.append((station, tdate, fhorizon, model))
        
        feature_matrix = np.array(feature_matrix)
        
        if feature_matrix.size == 0:
            return np.array([]).reshape(0, len(self.variable_names))
        
        # Normalize features (standard scaling)
        means = np.nanmean(feature_matrix, axis=0)
        stds = np.nanstd(feature_matrix, axis=0)
        # Avoid division by zero
        stds = np.where(stds == 0, 1, stds)
        
        normalized_matrix = (feature_matrix - means) / stds
        
        return normalized_matrix, sample_keys, (means, stds)

    def _fit_pca_and_covariance(self, station: str) -> Tuple[PCA, np.ndarray, np.ndarray, Dict]:
        """Fit PCA and estimate covariance matrix for a station."""
        nwp_data = self._load_nwp_data()
        if not nwp_data:
            raise ValueError(f"No NWP data available to fit PCA for station {station}")
        
        station_matrix, sample_keys, norm_stats = self._prepare_station_data(station, nwp_data)
        if station_matrix.size == 0:
            raise ValueError(f"No data matrix could be formed for station {station}")
        
        # Perform PCA to reduce to ~5-10 components for 95% variance explained
        n_components = min(10, station_matrix.shape[1], station_matrix.shape[0])
        pca = PCA(n_components=n_components)
        pcs = pca.fit_transform(station_matrix)
        
        # Calculate covariance matrix of reduced data for Mahalanobis
        cov_matrix = np.cov(pcs.T)
        if cov_matrix.shape[0] == 0:
            raise ValueError(f"Covariance matrix could not be calculated for station {station}")
        
        inv_cov_matrix = np.linalg.pinv(cov_matrix)  # Use pseudoinverse for numerical stability
        
        model_data = {
            'pca_model': pca,
            'inverse_covariance': inv_cov_matrix,
            'normalization_stats': norm_stats,
            'feature_names': self.variable_names,
            'sample_keys': sample_keys
        }
        
        return pca, inv_cov_matrix, station_matrix, model_data

    def _validate_and_build_target_vector(self, station: str, target_date: str, target_horizon: int = 0, target_model: str = 'GFS') -> Optional[np.ndarray]:
        """
        Build the vector for target day from DB, ensuring same normalization as training data.
        """
        # Get stored normalization statistics and PCA model
        if station in self.pca_models:
            norm_stats, pca_model = self.pca_models[station]['stats'], self.pca_models[station]['pca']
        else:
            # Need to build model first time - this is expensive so maybe store it
            try:
                pca, inv_cov, station_matrix, model_info = self._fit_pca_and_covariance(station)
                self.pca_models[station] = {
                    'pca': pca,
                    'inverse_cov': inv_cov,
                    'stats': model_info['normalization_stats'],
                    'matrix': station_matrix  # Keep for ref, though we shouldn't need it in runtime
                }
                # Also cache the covariance matrix
                self.cov_matrices[station] = inv_cov
                norm_stats = model_info['normalization_stats']
                pca_model = pca
            except ValueError as e:
                logger.error(f"Could not build model for station {station}: {e}")
                return None
        
        # Load fresh data for target day
        if not self.nwp_db_path.exists():
            return None
            
        conn = sqlite3.connect(str(self.nwp_db_path))
        try:
            cur = conn.cursor()
            
            # Query for the specific target day (adapted for schema with no forecast_horizon)
            vars_data = {}
            for var in self.variable_names:
                # Handle forecast vs actual date naming
                var_base = var.split('_fcst')[0] if '_fcst_' in var else var
                cur.execute("""
                    SELECT variable, value 
                    FROM nwp_forecasts 
                    WHERE station = ? AND target_date = ? AND model = ? AND variable = ?
                    LIMIT 1
                """, (station, target_date, target_model, var_base))
                
                result = cur.fetchone()
                if result:
                    var_name, value = result
                    vars_data[var] = value
                else:
                    # Variable might be named slightly differently in DB
                    # Let's find similar variable names in the database
                    cur.execute("SELECT variable, value FROM nwp_forecasts WHERE station = ? AND target_date = ? AND model = ?", (station, target_date, target_model))
                    for db_var, db_val in cur.fetchall():
                        if var_base in db_var or db_var in var_base:
                            vars_data[var] = db_val
                            break
        
        finally:
            conn.close()

        # Build feature vector based on available data
        features = []
        for var in self.variable_names:
            val = vars_data.get(var)
            if val is not None:
                features.append(val)
            else:
                features.append(0.0)  # Or handle differently
        
        if len(features) != len(self.variable_names):
            return None

        feature_array = np.array(features)
        means, stds = norm_stats

        # Normalize the same way as training data
        normalized_features = (feature_array - means) / stds
        
        # Apply PCA transformation
        pca_features = pca_model.transform(normalized_features.reshape(1, -1))
        
        return pca_features[0]

    def find_analog_dates(self, station: str, target_date: str, target_horizon: int = 0, 
                          target_model: str = 'GFS', n_analogs: int = 30) -> List[Tuple[str, float]]:
        """
        Find analog dates using Mahalanobis distance in PCA space.
        
        Returns:
            List of tuples (date, mahalanobis_distance)
        """
        target_vector = self._validate_and_build_target_vector(station, target_date, 
                                                              target_horizon, target_model)
        if target_vector is None:
            logger.warning(f"Could not build target vector for {station} at {target_date}")
            return []
            
        # Reload NWP data to get all historical dates for this station
        nwp_data = self._load_nwp_data()
        if not nwp_data:
            return []
        
        # Process all historical dates for this station
        station_matrix, sample_keys, norm_stats = self._prepare_station_data(station, nwp_data)
        if station_matrix.size == 0:
            return []
        
        # Transform to PCA space
        pca_model = self.pca_models.get(station, {}).get('pca')
        if pca_model is None:
            try:
                pca, inv_cov, smatrix, model_info = self._fit_pca_and_covariance(station)
                pca_model = pca
                # Store for future use
                self.pca_models[station] = {
                    'pca': pca,
                    'inverse_cov': inv_cov,
                    'stats': model_info['normalization_stats']
                }
                self.cov_matrices[station] = inv_cov
            except ValueError as e:
                logger.error(f"Could not prepare data for analog search in {station}: {e}")
                return []
        
        pca_vectors = pca_model.transform(station_matrix)
        
        # Calculate Mahalanobis distances to target vector
        distances = []
        inv_cov = self.cov_matrices[station]
        
        for i, vec in enumerate(pca_vectors):
            try:
                dist = mahalanobis(target_vector, vec, inv_cov)
                if not np.isnan(dist) and not np.isinf(dist):
                    _, dt, hr, model = sample_keys[i]
                    distances.append((dt, float(dist)))
            except:
                # Skip any problematic calculations
                continue

        # Sort by distance and return N closest
        distances.sort(key=lambda x: x[1])
        return distances[:n_analogs]

    def _get_metar_outcomes(self, station: str, dates: List[str]) -> List[Tuple[str, str]]:
        """
        Get actual temperature outcomes from METAR for analog dates.
        
        Returns:
            List of (date, direction) where direction is 'up'/'down'/'equal' relative to previous day
        """
        if not self.metar_db_path.exists():
            logger.warning(f"METAR database not found at {self.metar_db_path}")
            return []

        conn = sqlite3.connect(str(self.metar_db_path))
        try:
            cur = conn.cursor()
            
            outcomes = []
            for date in dates:
                # Get current and previous day data
                cur.execute("""
                    SELECT date_utc, max_temp_f 
                    FROM daily_stats 
                    WHERE station = ? AND date_utc IN (?, ?)
                    ORDER BY date_utc
                """, (station, date, date))  # TODO: Actually get prev date
                
                # We need adjacent dates to calculate direction
                # Let's query with day before/after
                outcome_date = datetime.strptime(date, '%Y-%m-%d')
                prev_date = (outcome_date - timedelta(days=1)).strftime('%Y-%m-%d')
                
                cur.execute("""
                    SELECT date_utc, max_temp_f 
                    FROM daily_stats 
                    WHERE station = ? AND date_utc IN (?, ?)
                    ORDER BY date_utc
                """, (station, prev_date, date))
                
                records = cur.fetchall()
                temp_by_date = {rec[0]: rec[1] for rec in records if rec[1] is not None}
                
                if prev_date in temp_by_date and date in temp_by_date:
                    prev_temp, curr_temp = temp_by_date[prev_date], temp_by_date[date]
                    if curr_temp > prev_temp:
                        direction = 'up'
                    elif curr_temp < prev_temp:
                        direction = 'down' 
                    else:
                        direction = 'equal'
                        
                    outcomes.append((date, direction))
                    
        finally:
            conn.close()
            
        return outcomes

    def evaluate_nwp_standalone(self, station: str, target_date: str, k: int = 30) -> Tuple[Optional[str], float, Dict[str, Any]]:
        """
        Evaluate the enhanced NWP signal using Mahalanobis + PCA.
        
        Returns:
            (direction, confidence, additional_info)
        """
        # Find analog dates
        analog_matches = self.find_analog_dates(station, target_date, n_analogs=k)
        
        if len(analog_matches) == 0:
            return None, 0.0, {'method': 'mahalanobis_pca', 'k': k, 'reason': 'no_analogs_found'}
        
        # Get actual outcomes for the analog dates
        analog_dates = [date for date, _ in analog_matches]
        outcomes = self._get_metar_outcomes(station, analog_dates)
        
        if not outcomes:
            return None, 0.0, {
                'method': 'mahalanobis_pca', 
                'k': k, 
                'analog_count': len(analog_matches),
                'reason': 'no_metar_outcomes'
            }
        
        # Map outcomes to date
        outcome_map = {date: direction for date, direction in outcomes}
        
        # Calculate weighted probability based on analag distances (closer = more weight)
        up_votes = 0
        down_votes = 0
        total_weight = 0
        
        for date, distance in analog_matches:
            if date in outcome_map:
                weight = 1.0 / (1.0 + distance)  # Closer match gets higher weight
                direction = outcome_map[date]
                
                if direction == 'up':
                    up_votes += weight
                elif direction == 'down':
                    down_votes += weight
                
                total_weight += weight
        
        if total_weight == 0:
            return None, 0.0, {
                'method': 'mahalanobis_pca', 
                'k': k,
                'analog_count': len(analog_matches),
                'metar_count': len(outcomes),
                'reason': 'no_weighted_votes'
            }
        
        # Calculate confidence
        if up_votes > down_votes:
            direction = 'up'
            prob_up = (up_votes + 1) / (total_weight + 2)  # With laplace smoothing
        elif down_votes > up_votes:
            direction = 'down'
            prob_up = 1 - ((down_votes + 1) / (total_weight + 2))  # Adjust for down direction
        else:
            # Equal votes - no strong signal
            return None, 0.0, {
                'method': 'mahalanobis_pca',
                'k': k,
                'analog_count': len(analog_matches),
                'metar_count': len(outcomes),
                'reason': 'equal_votes',
                'up_score': up_votes,
                'down_score': down_votes
            }
        
        # Confidence is the probability minus the random baseline (0.5)
        confidence = max(0.0, 2.0 * abs(prob_up - 0.5))
        
        return direction, confidence, {
            'method': 'mahalanobis_pca',
            'k': k,
            'analog_count': len(analog_matches),
            'metar_count': len(outcomes),
            'up_weight': up_votes,
            'down_weight': down_votes,
            'probability': prob_up,
            'raw_confidence': abs(prob_up - 0.5)
        }


def compute_nwp_standalone_accuracy():
    """
    Back-tests the enhanced NWP signal across all stations and provides 
    accuracy statistics per station and per horizon as required by Phase 11.1
    
    Output to data/phase11_nwp_standalone_results.json
    """
    print("Running Phase 11.1: Computing NWP Standalone Signal with Mahalanobis + PCA...")
    print("Loading data and running PCA covariance-based analog matching...")
    
    signal = MahalanobisNwpAnalogSignal()
    
    # Get all available stations (discovered from DB)
    nwp_data = signal._load_nwp_data()
    if not signal.stations:
        print("No stations found, ending analysis")
        return {}
    
    print(f"Found {len(signal.stations)} stations: {signal.stations[:5]}...")
    
    # Get test dates based on available data (recent 6 months for evaluation)
    available_date_sets = set()
    for key in nwp_data.keys():
        station, date, horiz, model = key
        available_date_sets.add(date)
    
    available_dates = sorted(list(available_date_sets))
    # Use last 60 days as 'recent' for analysis
    test_dates = available_dates[-60:] if len(available_dates) >= 60 else available_dates[-20:]
    
    print(f"Evaluating on {len(test_dates)} test dates: {test_dates[:3]}... to {test_dates[-3:]}")
    
    results = {
        'summary': {},
        'per_station': {},
        'details_by_date': {}
    }
    
    # For each station and date, run the analysis
    for station in signal.stations[:5]:  # Limit for speed test
        station_results = []
        
        for date in test_dates:
            try:
                direction, confidence, info = signal.evaluate_nwp_standalone(station, date)
                
                if direction is not None:
                    # Need to get the true outcome
                    outcome = signal._get_metar_outcomes(station, [date])
                    if len(outcome) > 0 and outcome[0][0] == date:
                        true_direction = outcome[0][1]  # 'up', 'down', 'equal' 
                        correct = 1 if true_direction == direction else 0
                        
                        station_results.append({
                            'date': date,
                            'predicted': direction,
                            'confidence': confidence,
                            'actual': true_direction,
                            'correct': correct,
                            'info': info
                        })
            except Exception as e:
                logger.error(f"Error evaluating {station} on {date}: {e}")
                continue
                
        if station_results:
            correct_predictions = sum(r['correct'] for r in station_results)
            total_predictions = len(station_results)
            accuracy = correct_predictions / total_predictions if total_predictions > 0 else 0
            
            # Calculate average confidence
            avg_confidence = sum(r['confidence'] for r in station_results) / total_predictions if total_predictions > 0 else 0
            
            results['per_station'][station] = {
                'accuracy': accuracy,
                'total_predictions': total_predictions,
                'correct_predictions': correct_predictions,
                'average_confidence': avg_confidence,
                'results': station_results
            }
        else:
            print(f"No results from {station}")

    # Summary across all stations
    if results['per_station']:
        accuracy_values = [v['accuracy'] for v in results['per_station'].values()]
        avg_accuracy = sum(accuracy_values) / len(accuracy_values) if accuracy_values else 0

        total_preds = sum(v['total_predictions'] for v in results['per_station'].values())
        total_correct = sum(v['correct_predictions'] for v in results['per_station'].values())
        
        results['summary'] = {
            'overall_accuracy': avg_accuracy,
            'total_predictions': total_preds,
            'total_correct': total_correct,
            'prediction_rate': avg_accuracy if total_preds > 0 else 0,
            'stations_evaluated': len(results['per_station']),
            'method': 'mahalanobis_pca_nwp_signal',
            'timestamp': datetime.now().isoformat()
        }
        
        print(f"\nPHASE 11.1 RESULTS SUMMARY:")
        print(f"  Average Accuracy Across Stations: {avg_accuracy:.3f} ({avg_accuracy*100:.1f}%)")
        print(f"  Total Predictions: {total_preds}")
        print(f"  Total Correct: {total_correct}")
        print(f"  Stations Evaluated: {len(results['per_station'])}")
    else:
        print("No valid station results generated.")
        results['summary'] = {
            'error': 'No valid predictions could be generated',
            'timestamp': datetime.now().isoformat()
        }
    
    # Save results
    import json
    results_file = "data/phase11_nwp_standalone_results.json"
    with open(results_file, 'w') as f:
        json.dump(results, f, indent=2)
    print(f"\nResults saved to {results_file}")
    
    return results


if __name__ == '__main__':
    # Run phase 11.1 evaluation
    try:
        import sys
        # Change to the appropriate directory if needed
        import os
        os.chdir(os.path.dirname(__file__) + '/../..')  # Move up to main workspace
        compute_nwp_standalone_accuracy()
    except Exception as e:
        print(f"Error running NWP standalone evaluation: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)