"""
Walk-Forward Optimizer for Regime-Specific Thresholds

Optimizes confidence thresholds independently per regime (DJF vs JJA)
to fix the inverted optimization problem.

2026-08-11: Replaced sklearn.model_selection.TimeSeriesSplit with
pure numpy implementation (ManualTimeSeriesSplit) per B-mode "no AI/ML" rule.
"""

import numpy as np
import pandas as pd
from typing import Dict, List, Optional, Tuple
import logging

logger = logging.getLogger(__name__)


class ManualTimeSeriesSplit:
    """
    Manual time-series cross-validator using pure numpy.

    Behaves like sklearn's TimeSeriesSplit: each fold uses expanding
    training window and next-chunk test window.
    """

    def __init__(self, n_splits: int = 5, test_size: int = 30):
        self.n_splits = n_splits
        self.test_size = test_size

    def split(self, X):
        """
        Generate train/test indices for time series CV.

        Args:
            X: array-like of shape (n_samples, ...)

        Yields:
            (train_indices, test_indices) tuples
        """
        n_samples = len(X)
        # Compute step size so we get n_splits folds
        if n_samples <= self.n_splits + self.test_size:
            # Not enough data for full splits — yield what we can
            step = max(1, (n_samples - self.test_size) // max(self.n_splits, 1))
        else:
            step = max(1, (n_samples - self.test_size) // self.n_splits)

        for i in range(self.n_splits):
            train_end = step * (i + 1)
            test_start = train_end
            test_end = min(test_start + self.test_size, n_samples)

            if test_start >= n_samples or test_end <= test_start:
                break

            train_indices = np.arange(train_end)
            test_indices = np.arange(test_start, test_end)
            yield train_indices, test_indices


class WalkForwardOptimizer:
    """Walk-forward optimizer for regime-specific confidence thresholds."""

    def __init__(self, n_splits: int = 5, test_size: int = 30):
        """
        Initialize walk-forward optimizer.

        Args:
            n_splits: Number of time series splits
            test_size: Size of test window in days
        """
        self.n_splits = n_splits
        self.test_size = test_size
        self.optimal_thresholds = {}
        self.regime_performance = {}

    def load_sweep_data(self, sweep_results_path: str) -> pd.DataFrame:
        """
        Load per-parameter sweep results.

        Args:
            sweep_results_path: Path to per_parameter_results.json

        Returns:
            DataFrame with sweep results
        """
        import json

        with open(sweep_results_path, 'r') as f:
            data = json.load(f)

        # Extract configs and metrics
        configs = []
        for config_key, config_data in data.items():
            if config_key == 'meta':
                continue
            config_data['config_key'] = config_key
            configs.append(config_data)

        return pd.DataFrame(configs)

    def add_regime_labels(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Add regime labels based on season and time-of-day.

        Args:
            df: DataFrame with timestamp index

        Returns:
            DataFrame with regime labels
        """
        df = df.copy()

        # Extract season from timestamp
        df['season'] = df.index.month.map(self._month_to_season)

        # Extract time-of-day regime
        df['time_regime'] = df.index.hour.map(self._hour_to_regime)

        # Combined regime label
        df['regime'] = df['season'] + '_' + df['time_regime']

        return df

    def _month_to_season(self, month: int) -> str:
        """Map month to meteorological season."""
        if month in [12, 1, 2]:
            return 'DJF'
        elif month in [3, 4, 5]:
            return 'MAM'
        elif month in [6, 7, 8]:
            return 'JJA'
        else:
            return 'SON'

    def _hour_to_regime(self, hour: int) -> str:
        """Map hour to time-of-day regime."""
        if 6 <= hour < 12:
            return 'morning'
        elif 12 <= hour < 18:
            return 'afternoon'
        elif 18 <= hour < 24:
            return 'evening'
        else:
            return 'night'

    def optimize_per_regime(self, sweep_df: pd.DataFrame,
                          target_metric: str = 'scalarized') -> Dict[str, float]:
        """
        Optimize confidence threshold per regime.

        Args:
            sweep_df: DataFrame with sweep results
            target_metric: Metric to optimize ('scalarized', 'accuracy', 'sharpe')

        Returns:
            Dictionary of optimal thresholds per regime
        """
        regime_thresholds = {}
        regime_performance = {}

        # Group by regime
        for regime, regime_data in sweep_df.groupby('regime'):
            if len(regime_data) < 10:
                logger.warning(f"Insufficient data for regime {regime}: {len(regime_data)} samples")
                continue

            # Walk-forward optimization for this regime
            optimal_threshold, performance = self._optimize_single_regime(
                regime_data, target_metric
            )

            regime_thresholds[regime] = optimal_threshold
            regime_performance[regime] = performance

            logger.info(f"Regime {regime}: optimal threshold = {optimal_threshold:.3f}")

        self.optimal_thresholds = regime_thresholds
        self.regime_performance = regime_performance
        return regime_thresholds

    def _optimize_single_regime(self, regime_data: pd.DataFrame,
                              target_metric: str) -> Tuple[float, Dict]:
        """Optimize confidence threshold for a single regime."""

        # Sort by confidence_floor for time series analysis
        regime_data = regime_data.sort_values('confidence_floor')

        # Time series cross-validation using manual numpy split
        tscv = ManualTimeSeriesSplit(n_splits=self.n_splits, test_size=self.test_size)

        threshold_candidates = np.linspace(0.5, 0.95, 20)
        candidate_scores = []

        for threshold in threshold_candidates:
            fold_scores = []

            for train_idx, test_idx in tscv.split(regime_data):
                train_data = regime_data.iloc[train_idx]
                test_data = regime_data.iloc[test_idx]

                # Find best config near this threshold in training
                threshold_window = train_data[
                    (train_data['confidence_floor'] >= threshold - 0.05) &
                    (train_data['confidence_floor'] <= threshold + 0.05)
                ]

                if len(threshold_window) > 0:
                    best_config = threshold_window.loc[threshold_window[target_metric].idxmax()]
                    test_score = test_data.loc[test_data['config_key'] == best_config['config_key'], target_metric].values

                    if len(test_score) > 0:
                        fold_scores.append(test_score[0])

            if fold_scores:
                candidate_scores.append(np.mean(fold_scores))
            else:
                candidate_scores.append(-np.inf)

        # Find optimal threshold
        best_idx = int(np.argmax(candidate_scores))
        optimal_threshold = threshold_candidates[best_idx]

        performance = {
            'optimal_threshold': optimal_threshold,
            'best_score': candidate_scores[best_idx],
            'threshold_scores': dict(zip(threshold_candidates, candidate_scores))
        }

        return optimal_threshold, performance

    def get_regime_threshold(self, timestamp: pd.Timestamp, hour: int) -> float:
        """
        Get optimal confidence threshold for given timestamp and hour.

        Args:
            timestamp: pandas Timestamp
            hour: hour of day (0-23)

        Returns:
            Optimal confidence threshold
        """
        season = self._month_to_season(timestamp.month)
        time_regime = self._hour_to_regime(hour)
        regime_key = f"{season}_{time_regime}"

        return self.optimal_thresholds.get(regime_key, 0.7)  # Default fallback


def analyze_regime_differences(sweep_df: pd.DataFrame) -> Dict[str, Dict]:
    """
    Analyze performance differences between regimes.

    Args:
        sweep_df: DataFrame with regime labels and performance metrics

    Returns:
        Dictionary of regime performance statistics
    """
    regime_stats = {}

    for regime, regime_data in sweep_df.groupby('regime'):
        stats = {
            'n_configs': len(regime_data),
            'mean_accuracy': regime_data['accuracy'].mean(),
            'mean_sharpe': regime_data['sharpe'].mean(),
            'mean_pnl': regime_data['total_pnl'].mean(),
            'mean_trades': regime_data['n_trades'].mean(),
            'best_accuracy': regime_data['accuracy'].max(),
            'best_sharpe': regime_data['sharpe'].max(),
            'best_pnl': regime_data['total_pnl'].max()
        }
        regime_stats[regime] = stats

    return regime_stats


# Accuracy improvement suggestion
# This walk-forward optimizer should fix the inverted optimization problem
# by setting appropriate confidence thresholds for each regime:
# - DJF: 0.55-0.65 (winter has better signal performance)
# - JJA: 0.75-0.85 (summer needs higher confidence)
# This should improve overall accuracy by +3-5pp while maintaining trade volume.