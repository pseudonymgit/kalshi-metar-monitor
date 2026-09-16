"""
PAVA (Pool Adjacent Violators Algorithm) — Pure numpy/scipy isotonic regression.

Replaces sklearn.isotonic.IsotonicRegression per B-mode "no AI/ML" rule.
Used by calibration_pipeline.py for walk-forward calibration.

Reference: Barlow, Bartholomew, Brenner & Brunk (1972), "Statistical Inference
under Order Restrictions", Wiley.
"""

import numpy as np
from scipy.optimize import minimize_scalar
from typing import Optional, Tuple


def _pava_isotonic_fit(x: np.ndarray, y: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """
    PAVA: Pool Adjacent Violators Algorithm for isotonic regression.

    Given points (x_i, y_i), finds the isotonic (non-decreasing) regression
    that minimizes sum((y_i - yhat_i)^2) subject to yhat_i <= yhat_{i+1}.

    Args:
        x: Sorted input values (1D array)
        y: Output values (1D array, same length as x)

    Returns:
        (x_unique, y_fitted) where y_fitted is non-decreasing and
        represents the isotonic regression at the unique sorted x values.
    """
    # Sort by x
    order = np.argsort(x)
    x_sorted = x[order]
    y_sorted = y[order]

    n = len(y_sorted)
    if n == 0:
        return np.array([]), np.array([])
    if n == 1:
        return x_sorted, y_sorted

    # PAVA blocks: each block = (sum_y, count, start_idx)
    blocks = []
    for i in range(n):
        blocks.append([y_sorted[i], 1, i])

    # Merge violators
    i = 0
    while i < len(blocks) - 1:
        mean_i = blocks[i][0] / blocks[i][1]
        mean_next = blocks[i + 1][0] / blocks[i + 1][1]
        if mean_i > mean_next:  # Violates monotonicity
            # Merge blocks
            blocks[i][0] += blocks[i + 1][0]  # sum_y
            blocks[i][1] += blocks[i + 1][1]  # count
            # Keep start_idx from first block
            blocks.pop(i + 1)
            # Step back to check if merge created new violation
            if i > 0:
                i -= 1
        else:
            i += 1

    # Build output
    x_unique = []
    y_fitted = []
    for block in blocks:
        mean = block[0] / block[1]
        start = block[2]
        end = start + block[1] if len(blocks) > blocks.index(block) + 1 else n
        xs = x_sorted[start:end]
        x_unique.append(float(np.mean(xs)))
        y_fitted.append(float(mean))

    return np.array(x_unique), np.array(y_fitted)


class PavaIsotonicRegressor:
    """
    Pure numpy/scipy isotonic regression via PAVA.

    Interface compatible with sklearn.isotonic.IsotonicRegression:
      - fit(X, y) -> self
      - transform([X]) -> array of calibrated values

    Supports out-of-bounds clipping and y_min/y_max constraints.
    """

    def __init__(self, out_of_bounds: str = 'clip',
                 y_min: Optional[float] = None,
                 y_max: Optional[float] = None):
        """
        Args:
            out_of_bounds: 'clip' or 'nan'. How to handle X values outside
                           training range. 'clip' uses nearest neighbor.
            y_min: Minimum output value (clamped after transform)
            y_max: Maximum output value (clamped after transform)
        """
        self.out_of_bounds = out_of_bounds
        self.y_min = y_min
        self.y_max = y_max
        self.x_train_ = None
        self.y_train_ = None
        self.fitted_ = False

    def fit(self, X, y):
        """
        Fit isotonic regression via PAVA.

        Args:
            X: Input values (1D array-like)
            y: Target values (1D array-like)

        Returns:
            self
        """
        x = np.asarray(X, dtype=float).ravel()
        y = np.asarray(y, dtype=float).ravel()

        if len(x) != len(y):
            raise ValueError(f"X and y must have same length, got {len(x)} vs {len(y)}")
        if len(x) == 0:
            raise ValueError("Cannot fit on empty data")

        # Sort by x, remove any NaN
        valid = ~(np.isnan(x) | np.isnan(y))
        x = x[valid]
        y = y[valid]

        if len(x) == 0:
            raise ValueError("No valid data after removing NaN")

        xs, ys = _pava_isotonic_fit(x, y)

        self.x_train_ = xs
        self.y_train_ = ys
        self.fitted_ = True
        return self

    def transform(self, T):
        """
        Apply isotonic regression: map new X values to calibrated values.

        Args:
            T: Input values (array-like)

        Returns:
            Array of calibrated values (non-decreasing interpolation)
        """
        if not self.fitted_:
            raise RuntimeError("Must call fit() before transform()")

        t = np.asarray(T, dtype=float).ravel()
        result = np.empty_like(t)

        for i, val in enumerate(t):
            result[i] = self._predict_single(val)

        # Apply y_min/y_max clamping
        if self.y_min is not None:
            result = np.maximum(result, self.y_min)
        if self.y_max is not None:
            result = np.minimum(result, self.y_max)

        return result

    def _predict_single(self, val: float) -> float:
        """Predict for a single value using step-function interpolation."""
        xs = self.x_train_
        ys = self.y_train_

        if len(xs) == 0:
            return val
        if len(xs) == 1:
            return float(ys[0])

        # Handle out-of-bounds
        if val < xs[0]:
            if self.out_of_bounds == 'clip':
                return float(ys[0])
            else:
                return float('nan')
        if val > xs[-1]:
            if self.out_of_bounds == 'clip':
                return float(ys[-1])
            else:
                return float('nan')

        # Find the bin — PAVA gives us step-function (right-continuous)
        # For value between xs[i] and xs[i+1], use ys[i]
        for j in range(len(xs) - 1):
            if xs[j] <= val < xs[j + 1]:
                return float(ys[j])

        # Exactly at the last point
        if val == xs[-1]:
            return float(ys[-1])

        return float(ys[-1])