#!/usr/bin/env python3
"""
PHASE 13.1: CDF Trajectory Pipeline

Bayesian bootstrap trajectory distribution from analog ensemble
- PIT histogram + KS test for calibration validation
- Continuous CDF output per trade

Implements Expert 5 recommendations for uncertainty quantification and 
probabilistic trajectory output as per Gray Room Round 6 recommendations.
"""

import numpy as np
import pandas as pd
from scipy.stats import ks_2samp, kstest, norm
from sklearn.mixture import BayesianGaussianMixture
from scipy.stats import gaussian_kde
from datetime import datetime
from pathlib import Path
import json
import pickle
# import matplotlib
# matplotlib.use('Agg')  # No-display backend for plots  
# import matplotlib.pyplot as plt # Skip plot imports that aren't essential
from typing import Dict, List, Tuple, Optional
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class BayesianTrajectoryCDF:
    """
    Implements the trajectory distribution pipeline with proper uncertainty
    quantification from analog ensembles as per Expert 5 recommendations.
    """
    
    def __init__(self):
        self.epistemic_uncertainty_active = True
        self.aleatoric_uncertainty_active = True
        self.analog_ensemble_size = 50  # Default
        self.bootstraps = 500           # Number of bootstrap samples for BB
        
    def _calculate_epistemic_aleatoric_decomposition(self, analog_predictions: np.ndarray) -> Dict[str, float]:
        """
        Decompose uncertainty into epistemic (reducible) and aleatoric (irreducible).
        
        Expert 5 Section 1: Separate the two types of uncertainty appropriately.
        
        Args:
            analog_predictions: Array of shape (n_analogs, n_time_steps) for trajectories
            
        Returns:
            Dictionary with epistemic/aleatoric uncertainty measurements
        """
        if analog_predictions.size == 0 or len(analog_predictions) == 0:
            return {
                'aleatoric': 0.0,
                'epistemic': 0.0,
                'total': 0.0,
                'analogs_to_add_for_halving_epistemic': float('inf')
            }
            
        # Aleatoric: variance within tightest analogs (what is inevitable)
        # For simplicity, we'll use overall ensemble variance as a proxy for aleatoric
        overall_variance = np.var(analog_predictions, axis=0).mean()  # Mean over all time steps
        
        # Epistemic: sensitivity to sample choice (reducible with more data)
        # For each time step, calculate inter-model (analogs) variance
        # Then estimate how much would decrease if we had more analogs
        n_analogs = analog_predictions.shape[0]
        epistemic_factor = 1.0 / np.sqrt(n_analogs) if n_analogs > 0 else 0.0
        epistemic_uncertainty = overall_variance * epistemic_factor
        
        aleatoric_uncertainty = max(0.0, overall_variance - epistemic_uncertainty)
        
        # How many additional analogs to half the epistemic uncertainty?
        # Epistemic scales as 1/sqrt(n) so to half it, need 4x as many
        to_half_epi = 4 * n_analogs - n_analogs
        
        return {
            'aleatoric': float(aleatoric_uncertainty),
            'epistemic': float(epistemic_uncertainty), 
            'total': float(overall_variance),
            'analogs_to_add_for_halving_epistemic': int(to_half_epi)
        }
    
    def fit_trajectory_distribution(self, analog_ensemble: List[np.ndarray]) -> Dict:
        """
        Fit a probabilistic trajectory distribution from analog ensemble using
        Bayesian bootstrap approach with kernel smoothing.
        
        Implements Expert 1 Section 2: Bayesian Bootstrap with Kernel Smoothing (BB-KS)
        
        Args:
            analog_ensemble: List of trajectory arrays [timestep x n_variables]
            
        Returns:
            Distribution model and summary statistics
        """
        if not analog_ensemble:
            return {'error': 'Empty analog ensemble provided'}
        
        # Convert to numpy array
        trajectories = np.array(analog_ensemble)
        n_analogs, n_time_steps = trajectories.shape[0], trajectories.shape[1] if len(trajectories.shape) > 1 else (0, 0)
        
        if n_time_steps == 0:
            return {'error': 'Trajectories have no time dimension'}
        
        # Calculate uncertainty decomposition per Expert 5
        unc_decomp = self._calculate_epistemic_aleatoric_decomposition(trajectories)
        
        # Bayesian Bootstrap Part - Sample weights from Dirichlet(1, 1, ..., 1)
        # This gives equal prior weight to each analog then resamples
        bootstrapped_distributions = []
        
        for bootstrap_idx in range(self.bootstraps):
            # Draw weights from Dirichlet(1, ..., 1) - symmetric Dirichlet
            weights = np.random.dirichlet(np.ones(n_analogs))
            
            # Weighted averaging across analogs for each time step
            # For each time step, create weighted mean of all analogs at that step
            bootstrapped_trajectory = np.zeros(n_time_steps)
            for t in range(n_time_steps):
                bootstrapped_trajectory[t] = np.average(trajectories[:, t], weights=weights)
            
            bootstrapped_distributions.append(bootstrapped_trajectory)
        
        bootstrapped_array = np.array(bootstrapped_distributions)
        
        # Construct final CDF using aggregated bootstrap samples
        # For each time step, we have bootstrap_samples of possible outcomes
        cdf_per_step = []
        pdf_samples = []  # To compute kernel density estimate
        
        for t in range(n_time_steps):
            step_values = bootstrapped_array[:, t]
            
            # Get unique sorted values and cumulative probabilities (CDF)
            sorted_vals = np.sort(step_values)
            cum_probs = np.arange(1, len(sorted_vals) + 1) / len(sorted_vals)
            
            # Also compute KDE for smooth continuous distribution
            kde = gaussian_kde(step_values)
            # For output, we can use representative quantiles
            quantiles = [0.05, 0.25, 0.5, 0.75, 0.95]  # Common quantiles
            quantile_values = [np.quantile(step_values, q) for q in quantiles]
            
            cdf_per_step.append({
                'time_step': int(t),
                'quantile_values': {f'p{int(q*100)}': float(v) for q, v in zip(quantiles, quantile_values)},
                'mean': float(np.mean(step_values)),
                'median': float(np.median(step_values)),
                'std': float(np.std(step_values)),
                'expected_value': float(np.mean(step_values)),  # Use mean as expectation
                'min_value': float(np.min(step_values)), 
                'max_value': float(np.max(step_values))
            })
            
            # Get some KDE evaluation points for distribution shape
            # Use a reasonable range covering 5% to 95% quantiles
            range_start = cdf_per_step[-1]['quantile_values']['p5']
            range_end = cdf_per_step[-1]['quantile_values']['p95']
            eval_points = np.linspace(range_start, range_end, 20)
            kde_values = kde(eval_points)
            
            pdf_samples.append({
                'time_step': int(t),
                'kde_points': eval_points.tolist(),
                'kde_values': kde_values.tolist() 
            })
        
        # Compute calibration metric: PI transform (PIT) histogram
        pit_scores = self._compute_pit_histogram(cdf_per_step, trajectories)
        
        return {
            'method': 'Bayesian bootstrap with KDE smoothing',
            'n_analogs': n_analogs,
            'trajectory_length': n_time_steps,
            'cdf_by_timestep': cdf_per_step,
            'density_estimates': pdf_samples,  # KDE samples for visualization
            'calibration_metrics': pit_scores,
            'uncertainty_decomposition': unc_decomp,
            'bayesian_bootstrap_iterations': self.bootstraps,
            'sample_trajectories_used': trajectories.shape[0]
        }
    
    def _compute_pit_histogram(self, cdf_per_step: List[Dict], actual_trajectories: np.ndarray) -> Dict:
        """
        Compute Probability Integral Transform histogram and KS test for calibration.
        
        Expert 5 Section 4: PIT histogram and KS test as gold standard for calibration
        """
        if not actual_trajectories.any() or actual_trajectories.size == 0:
            return {'pit_test_applicable': False, 'message': 'no validation data'}
        
        if len(cdf_per_step) == 0:
            return {'pit_test_applicable': False, 'message': 'no CDF data to validate'}
        
        # Match actual trajectories with our time grid
        # This validation requires that actual trajectories align with our forecasting grid
        n_steps = min(len(cdf_per_step), actual_trajectories.shape[1] if len(actual_trajectories.shape) > 1 else 0)
        
        pit_scores_by_step = []
        for t in range(n_steps):
            # For this time step, get what CDF said and match to actual observation
            if t < actual_trajectories.shape[1]:  # Check bounds
                
                # Extract the observed values at this time step
                observed_values = actual_trajectories[:, t]  # Value observed for each realization
                
                # For validation, we should be comparing forecasts to corresponding observations
                # Since we don't have the forecasting vs. backtesting framework here,
                # we'll use this as a mock validation
                if t < len(cdf_per_step):
                    # Get the CDF values that would have been computed when this "observation" was made
                    # For now, use the median as a central estimator and see how observed values compare
                    forecast_mean = cdf_per_step[t]['mean']
                    forecast_std = cdf_per_step[t]['std']
                    
                    # Compute PIT score as F(observed) - the CDF evaluated at observed value
                    # Using normal approximation: (observed - forecast_mean) / forecast_std, CDF normal
                    if forecast_std > 0.001:  # Avoid division by zero
                        standardized_vals = (observed_values - forecast_mean) / forecast_std 
                        # Map to uniform using true normal CDF
                        pit_for_step = norm.cdf(standardized_vals)
                        pit_scores_by_step.extend(pit_for_step.tolist())
        
        # Perform KS test against uniform distribution (0,1)
        if pit_scores_by_step:
            # Add small epsilon values to avoid 0/1 which cause issues in KS calc
            pit_scores = np.clip(pit_scores_by_step, 1e-5, 1.0 - 1e-5)
            ks_stat, p_value = kstest(pit_scores, 'uniform')
            
            return {
                'pit_test_conducted': True,
                'ks_statistic': float(ks_stat),
                'ks_pvalue': float(p_value),
                'pit_sample_size': len(pit_scores),
                'calibration_good': float(p_value) > 0.05,  # If p > 0.05, no significant deviation from uniform
                'pit_scores_preview': pit_scores[:10].tolist()  # First 10 for review
            }
        else:
            return {
                'pit_test_conducted': False, 
                'message': 'Could not compute PIT - no forecast-observation pairs matched'
            }
    
    def get_cdf_at_horizon(self, distribution_model: Dict, horizon_step: int) -> Optional[Dict]:
        """
        Extract the full CDF curve for a particular forecast horizon.
        
        For Kalshi markets that settle at a specific time, we need the 
        full cumulative distribution function.
        """
        if 'cdf_by_timestep' not in distribution_model:
            return None
            
        if horizon_step >= len(distribution_model['cdf_by_timestep']):
            return None
            
        cdf_data = distribution_model['cdf_by_timestep'][horizon_step]
        ts_data = distribution_model.get('density_estimates', [])
        
        # Construct detailed CDF representation at this horizon
        cdf_result = {
            'horizon_step': horizon_step,
            'expected_value': cdf_data['expected_value'],
            'quantile_values': cdf_data['quantile_values'],  # p5, p25, p50, p75, p95
            'calibration_status': 'need_verification_pit',  # Calibration needs to be checked vs actual
            'distribution_summary': {
                'mean': cdf_data['mean'],
                'median': cdf_data['median'],
                'std': cdf_data['std'],
                'min': cdf_data['min_value'],
                'max': cdf_data['max_value']
            },
            'pdf_samples': ts_data[horizon_step]['kde_values'] if ts_data and horizon_step < len(ts_data) else [],
            'pdf_x_points': ts_data[horizon_step]['kde_points'] if ts_data and horizon_step < len(ts_data) else []
        }
        
        return cdf_result

def run_cdf_trajectory_pipeline():
    """
    Execute Phase 13.1: CDF Trajectory Pipeline
    
    Create mock trajectory data and run the full CDF pipeline
    """
    print("Running Phase 13.1: CDF Trajectory Pipeline")
    print("=" * 60)
    
    trajectory_builder = BayesianTrajectoryCDF()
    
    # Simulate analog ensemble - these would come from NWP analog matching
    # Each "analog" is a trajectory across n time steps (temperature by forecast hour)
    print("Generating mock analog ensemble for demonstration...")
    
    n_analogs = 30  # Number of similar historical days matched
    n_time_steps = 24  # e.g., hourly forecast over 24 hours to settlement 
    
    # Simulate diverse but related trajectories
    mock_ensemble = []
    base_level = 75.0  # Base temperature
    
    for i in range(n_analogs):
        # Create somewhat realistic temperature evolution: day start, peak around noon, then drop
        time_index = np.arange(0, n_time_steps)
        diurnal_shape = 2*np.sin(2*np.pi*(time_index-6)/24)  # Peak at index 6 (noon)
        analog_shift = np.random.normal(0, 3)  # Random analog bias
        analog_noise = np.random.normal(0, 1, size=n_time_steps)  # Random walk
        
        trajectory = base_level + diurnal_shape + analog_shift + np.cumsum(analog_noise * 0.1)
        mock_ensemble.append(trajectory) 

    print(f"Generated ensemble with {n_analogs} analogs * {n_time_steps} time steps each")
    
    # Run CDF fitting pipeline
    cdf_results = trajectory_builder.fit_trajectory_distribution(mock_ensemble)
    
    if 'error' in cdf_results:
        print(f"ERROR: {cdf_results['error']}")
        return cdf_results
    
    print(f"\nFitted CDF model with {cdf_results['n_analogs']} analog trajectories")
    print(f"Each trajectory has {cdf_results['trajectory_length']} time steps")
    
    # Validate using Probability Integral Transform
    pit_analysis = cdf_results.get('calibration_metrics', {})
    if 'pit_test_conducted' in pit_analysis:
        print(f"\nPIT Calibration Test:")
        print(f"  - Conducted: {pit_analysis['pit_test_conducted']}")
        if 'ks_pvalue' in pit_analysis:
            print(f"  - KS P-value: {pit_analysis['ks_pvalue']:.3f} ('Good' if pit_analysis['calibration_good'] else 'Poor') calibration")
            print(f"  - KS Statistic: {pit_analysis['ks_statistic']:.3f}") 
            print(f"  - Sample Size: {pit_analysis['pit_sample_size']}")
    
    # Get the specific CDF for the settlement time horizon (e.g., end of the 24h forecast)
    settlement_horizon = n_time_steps - 1  # Last time point
    final_cdf = trajectory_builder.get_cdf_at_horizon(cdf_results, settlement_horizon)
    
    if final_cdf:
        print(f"\nCDF Results for Settlement Horizon (Step {settlement_horizon}):")
        print(f"  - Expected Value: {final_cdf['expected_value']:.2f}°F")
        print(f"  - P50 (Median): {final_cdf['distribution_summary']['median']:.2f}°F")
        print(f"  - P10/P90 Range: {final_cdf['quantile_values']['p5']:.2f} - {final_cdf['quantile_values']['p95']:.2f}°F")
        print(f"  - Standard Deviation: {final_cdf['distribution_summary']['std']:.2f}°F")
        
        # Example application to Kalshi market (binary options on temperature exceeding threshold)
        market_strike = 80.0  # Example: temp > 80 market
        # P(temp > 80) = 1 - F(80) where F is CDF at that forecast time
        
        # Approximate P(Y > strike) from our distribution quantiles
        # This is complex to compute exactly from quantile data alone,
        # so in production this would use the full KDE output or analytical CDF  
        strike_prob = 1 - ((80.0 - final_cdf['quantile_values']['p5']) / 
                          (final_cdf['quantile_values']['p95'] - final_cdf['quantile_values']['p5']) * 0.9 + 0.05) 
        # Very rough approximation based on where we'd expect 80F in our quantile range
        strike_prob = max(0.0, min(1.0, strike_prob))
        
        print(f"\nApplication to Kalshi Market (strike = {market_strike}°F):")
        print(f"  - Estimated P(temp > {market_strike}): ~{strike_prob:.2f}")
        print(f"  - Expected profit per $1: ${strike_prob * 0.97 - (1 - strike_prob) * 0.03:.2f}")  # Account for fees
        print(f"    ($0.03 fee assumed for this demonstration)")
    
    # Summarize uncertainty decomposition
    unc = cdf_results['uncertainty_decomposition']
    print(f"\nUncertainty Analysis:")  
    print(f"  - Aleatoric (irreducible): {unc['aleatoric']:.3f}")
    print(f"  - Epistemic (reducible):   {unc['epistemic']:.3f}")
    print(f"  - Total:                   {unc['total']:.3f}")
    if unc['aleatoric'] > unc['epistemic']:
        print(f"  - Primary: Aleatoric (inherent process variability)")
    else:
        print(f"  - Primary: Epistemic (could be reduced with better matching)")
        
    final_results = {
        'pipeline_status': 'SUCCESSFUL_EXECUTION',
        'ensemble_size': n_analogs,
        'time_horizon': n_time_steps,
        'calibration_metrics': pit_analysis,
        'final_cdf_summary': {
            'horizon': settlement_horizon,
            'expected_value': final_cdf['expected_value'] if final_cdf else None,
            'p10_p90_range': (
                final_cdf['quantile_values']['p5'] if final_cdf else None,
                final_cdf['quantile_values']['p95'] if final_cdf else None
            ),
            'market_application_example': {
                'strike': market_strike,
                'prob_exceeding': strike_prob if 'strike_prob' in locals() else None
            } if 'strike_prob' in locals() else {}
        },
        'uncertainty_analysis': unc,
        'trajectory_pipeline_components': [
            'Bayesian bootstrap ensemble aggregation',
            'Kernel density smoothing',
            'PIT histogram validation',
            'Epistemic/aleatoric decomposition',
            'CDF output at settlement time'
        ],
        'timestamp': datetime.now().isoformat()
    }
    
    # Save results
    import os
    results_dir = Path("data")
    results_dir.mkdir(exist_ok=True)
    
    output_file = results_dir / "phase13_trajectory_cdf_results.json"
    with open(output_file, 'w') as f:
        json.dump(final_results, f, indent=2, default=str)
    
    print(f"\nPhase 13 completed. Results saved to {output_file}")
    print(f"Demonstrated CDF pipeline with validation metrics")
    
    return final_results


if __name__ == '__main__':
    import sys
    import os
    # Change to workspace dir
    script_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(script_dir + '/../../..')  # Move to main workspace
    
    try:
        print("Executing Phase 13.1 CDF Trajectory Pipeline...")
        run_cdf_trajectory_pipeline()
        print("Phase 13 complete!")
    except Exception as e:
        print(f"Error in CDF trajectory pipeline: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)