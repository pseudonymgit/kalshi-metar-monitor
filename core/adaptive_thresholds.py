"""
Adaptive Confidence Threshold System — First-Principles (FP 6.4)

Bayesian Beta-Bernoulli posterior + dual EMA threshold controller.

Replaces v1 step-function. Key features:
    - Beta-Bernoulli conjugate prior for per-(signal, station) accuracy
    - 70% one-sided lower credible bound as base threshold
    - Dual EMA (15d fast + 90d slow) for seasonal baseline
    - Momentum dampening (max 5pp/day change)
    - Contraction mapping convergence proof (see FP-ADAPTIVE-THRESHOLDS.md)

Usage:
    from core.adaptive_thresholds import AdaptiveThresholdRegistry

    registry = AdaptiveThresholdRegistry()
    threshold = registry.get_threshold('gaussian_v2', 'KDEN')
    registry.record_outcome('gaussian_v2', 'KDEN', 0.72, True)
    passed = registry.filter_signals(signal_list)

B-Mode R8 Cycle 4.5: Replaced v1 step-function with Bayesian controller.
"""

import math
import json
import logging
import os
import sqlite3
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Per-Signal Configuration (FP 6.4 Section 6 — Signal Registry)
# ─────────────────────────────────────────────────────────────────────

@dataclass
class SignalConfig:
    """Per-signal adaptive threshold configuration."""
    tau_min: float = 0.55
    tau_max: float = 0.90
    fast_halflife_days: int = 25      # Fast EMA halflife
    slow_halflife_days: int = 100     # Slow EMA halflife (seasonal baseline)
    seasonal_gamma: float = 0.30      # Seasonal adjustment factor
    prior_n: int = 50                 # Prior effective sample size (prior_alpha = prior_beta = prior_n/2)
    description: str = ""


# Default signal configurations from FP 6.4 Section 6.1
SIGNAL_CONFIGS: Dict[str, SignalConfig] = {
    'calendar_climatology': SignalConfig(
        tau_min=0.55, tau_max=0.90, fast_halflife_days=30, slow_halflife_days=120,
        seasonal_gamma=0.30, prior_n=60, description="Daily, every station, 70-72% accuracy"
    ),
    'gaussian': SignalConfig(
        tau_min=0.55, tau_max=0.90, fast_halflife_days=25, slow_halflife_days=100,
        seasonal_gamma=0.30, prior_n=50, description="Daily, most stations, 71-73% accuracy"
    ),
    'gaussian_v2': SignalConfig(
        tau_min=0.55, tau_max=0.90, fast_halflife_days=25, slow_halflife_days=100,
        seasonal_gamma=0.30, prior_n=50, description="Daily, most stations, 71-73% accuracy"
    ),
    'pressure_delta': SignalConfig(
        tau_min=0.55, tau_max=0.90, fast_halflife_days=20, slow_halflife_days=90,
        seasonal_gamma=0.25, prior_n=40, description="Daily, most stations, 65-68% accuracy"
    ),
    'forecast_disagreement': SignalConfig(
        tau_min=0.55, tau_max=0.85, fast_halflife_days=15, slow_halflife_days=60,
        seasonal_gamma=0.20, prior_n=30, description="NWP-based, model run, 62-65% accuracy"
    ),
    'spike_reversion': SignalConfig(
        tau_min=0.55, tau_max=0.85, fast_halflife_days=20, slow_halflife_days=90,
        seasonal_gamma=0.20, prior_n=30, description="Derivative, ~20% of days, 60-65% accuracy"
    ),
    'goldilocks': SignalConfig(
        tau_min=0.55, tau_max=0.85, fast_halflife_days=20, slow_halflife_days=90,
        seasonal_gamma=0.20, prior_n=30, description="Derivative, daily, 60-65% accuracy"
    ),
    'wind_direction_shift': SignalConfig(
        tau_min=0.55, tau_max=0.85, fast_halflife_days=20, slow_halflife_days=90,
        seasonal_gamma=0.20, prior_n=25, description="Daily, most stations"
    ),
    'frontal_passage_intraday': SignalConfig(
        tau_min=0.55, tau_max=0.80, fast_halflife_days=15, slow_halflife_days=90,
        seasonal_gamma=0.15, prior_n=15, description="Observational, ~10% of days, 58-63%"
    ),
    'frontal_detector': SignalConfig(
        tau_min=0.55, tau_max=0.80, fast_halflife_days=15, slow_halflife_days=90,
        seasonal_gamma=0.15, prior_n=15, description="Physical, ~15% of days, 60-65%"
    ),
    'dewpoint_depression': SignalConfig(
        tau_min=0.55, tau_max=0.80, fast_halflife_days=15, slow_halflife_days=90,
        seasonal_gamma=0.15, prior_n=20, description="Physical, ~30% of days, 60-64%"
    ),
    'temperature_advection': SignalConfig(
        tau_min=0.55, tau_max=0.85, fast_halflife_days=20, slow_halflife_days=60,
        seasonal_gamma=0.20, prior_n=25, description="NWP-based, daily, 61-64%"
    ),
    'persistence_signal': SignalConfig(
        tau_min=0.55, tau_max=0.80, fast_halflife_days=10, slow_halflife_days=45,
        seasonal_gamma=0.15, prior_n=20, description="Daily, every station, 58-62%"
    ),
    'spread_based_entry': SignalConfig(
        tau_min=0.55, tau_max=0.85, fast_halflife_days=20, slow_halflife_days=90,
        seasonal_gamma=0.20, prior_n=20, description="Derivative, ~20% of days, 60-65%"
    ),
    'nowcasting': SignalConfig(
        tau_min=0.55, tau_max=0.80, fast_halflife_days=15, slow_halflife_days=90,
        seasonal_gamma=0.15, prior_n=15, description="Nowcasting, ~10% of days"
    ),
}

# Default config for unknown signals
DEFAULT_CONFIG = SignalConfig(
    tau_min=0.55, tau_max=0.85, fast_halflife_days=20, slow_halflife_days=90,
    seasonal_gamma=0.20, prior_n=30, description="Default for unregistered signals"
)


# ─────────────────────────────────────────────────────────────────────
# Station-Specific Overrides (FP 6.4 Section 6.3)
# ─────────────────────────────────────────────────────────────────────

# Additive adjustments to tau_min and tau_max per (station, signal)
STATION_OVERRIDES: Dict[Tuple[str, str], Dict[str, float]] = {
    ('KLAX', 'calendar_climatology'): {'tau_min': 0.03, 'tau_max': 0.03},
    ('KLAX', 'gaussian'): {'tau_min': -0.02, 'tau_max': -0.02},
    ('KLAX', 'pressure_delta'): {'tau_min': 0.01, 'tau_max': 0.01},
    ('KLAX', 'nowcasting'): {'tau_min': 0.05, 'tau_max': 0.05},
    ('KORD', 'calendar_climatology'): {'tau_min': -0.01, 'tau_max': -0.01},
    ('KORD', 'gaussian'): {'tau_min': 0.02, 'tau_max': 0.02},
    ('KATL', 'calendar_climatology'): {'tau_min': -0.02, 'tau_max': -0.02},
    ('KATL', 'gaussian'): {'tau_min': -0.01, 'tau_max': -0.01},
    ('KATL', 'nowcasting'): {'tau_min': -0.03, 'tau_max': -0.03},
    ('KDEN', 'calendar_climatology'): {'tau_min': 0.01, 'tau_max': 0.01},
    ('KDEN', 'gaussian'): {'tau_min': 0.03, 'tau_max': 0.03},
    ('KDEN', 'pressure_delta'): {'tau_min': 0.02, 'tau_max': 0.02},
    ('KDEN', 'nowcasting'): {'tau_min': -0.02, 'tau_max': -0.02},
}


# ─────────────────────────────────────────────────────────────────────
# AdaptiveThresholdController — per (signal, station)
# ─────────────────────────────────────────────────────────────────────

class AdaptiveThresholdController:
    """
    Beta-Bernoulli Bayesian threshold controller for a single (signal, station) pair.

    FP 6.4 Section 4: Dual EMA + Beta posterior + momentum dampening.
    """

    def __init__(self, signal_name: str, station: str,
                 config: Optional[SignalConfig] = None,
                 prior_alpha: Optional[float] = None,
                 prior_beta: Optional[float] = None):
        self.signal = signal_name
        self.station = station
        self.config = config or SIGNAL_CONFIGS.get(signal_name, DEFAULT_CONFIG)

        # Beta prior parameters
        prior_n = self.config.prior_n
        self.prior_alpha = prior_alpha if prior_alpha is not None else prior_n / 2.0
        self.prior_beta = prior_beta if prior_beta is not None else prior_n / 2.0

        # Dual EMA (initialized at ensemble baseline ~0.67)
        self.fast_ema = 0.67
        self.slow_ema = 0.67

        # Current threshold (conservative start)
        self.current_threshold = max(
            self.config.tau_min + self._station_offset('tau_min', 0.0),
            self.config.tau_min + 0.1
        )

        # Observation counts
        self.total_observations = 0
        self.correct_observations = 0

        # Lambda factors for EMAs
        self._lambda_fast = 1.0 - math.exp(-math.log(2) / self.config.fast_halflife_days)
        self._lambda_slow = 1.0 - math.exp(-math.log(2) / self.config.slow_halflife_days)

        logger.debug(
            "Created controller: %s/%s (tau=[%.2f, %.2f], fast_hl=%dd, slow_hl=%dd)",
            signal_name, station,
            self.config.tau_min, self.config.tau_max,
            self.config.fast_halflife_days, self.config.slow_halflife_days
        )

    def _station_offset(self, param: str, default: float = 0.0) -> float:
        """Get station-specific threshold offset."""
        key = (self.station, self.signal)
        override = STATION_OVERRIDES.get(key)
        if override and param in override:
            return override[param]
        # Try reverse key (signal, station)
        key_rev = (self.signal, self.station)
        override = STATION_OVERRIDES.get(key_rev)
        if override and param in override:
            return override[param]
        return default

    def update(self, calibrated_confidence: float, was_correct: bool) -> None:
        """
        Update posteriors after a settled trade.

        Args:
            calibrated_confidence: Calibrated P(correct) from calibration pipeline (0.05-0.95)
            was_correct: Whether the prediction was correct
        """
        correct_bit = 1.0 if was_correct else 0.0

        # Update dual EMAs
        self.fast_ema = self._lambda_fast * correct_bit + (1.0 - self._lambda_fast) * self.fast_ema
        self.slow_ema = self._lambda_slow * correct_bit + (1.0 - self._lambda_slow) * self.slow_ema

        # Update Beta posterior counts
        self.total_observations += 1
        if was_correct:
            self.correct_observations += 1

        # Recompute threshold
        self._recompute_threshold()

    def _recompute_threshold(self) -> None:
        """Compute threshold from current Beta posterior (FP 6.4 Section 4.2)."""
        alpha = self.prior_alpha + self.correct_observations
        beta = self.prior_beta + (self.total_observations - self.correct_observations)
        n_eff = self.prior_alpha + self.prior_beta + self.total_observations

        # Need minimum data to avoid wild swings
        if n_eff < 5:
            return

        # Posterior mean and variance
        total = alpha + beta
        theta_mean = alpha / total
        theta_var = (alpha * beta) / (total * total * (total + 1.0))
        theta_std = math.sqrt(theta_var) if theta_var > 0 else 0.01

        # 70% one-sided lower credible bound (z = 0.524)
        z = 0.524
        lower_bound = theta_mean - z * theta_std

        # Seasonal adjustment factor
        seasonal_diff = theta_mean - self.slow_ema
        adjustment = self.config.seasonal_gamma * seasonal_diff

        # Raw threshold
        raw = lower_bound + adjustment

        # Clamp to bounds (with station-specific offsets)
        tau_min = self.config.tau_min + self._station_offset('tau_min', 0.0)
        tau_max = self.config.tau_max + self._station_offset('tau_max', 0.0)
        clamped = max(tau_min, min(tau_max, raw))

        # Momentum dampening: max 5pp change per day (FP 6.4 Section 4.3)
        max_step = 0.05
        prev = self.current_threshold
        self.current_threshold = max(prev - max_step, min(prev + max_step, clamped))

    def get_status(self) -> dict:
        """Return current status for monitoring."""
        return {
            'signal': self.signal,
            'station': self.station,
            'current_threshold': round(self.current_threshold, 4),
            'fast_ema': round(self.fast_ema, 4),
            'slow_ema': round(self.slow_ema, 4),
            'total_observations': self.total_observations,
            'correct_observations': self.correct_observations,
            'empirical_accuracy': round(
                self.correct_observations / max(self.total_observations, 1), 4
            ),
            'tau_min': self.config.tau_min,
            'tau_max': self.config.tau_max,
        }


# ─────────────────────────────────────────────────────────────────────
# AdaptiveThresholdRegistry — manages all controllers
# ─────────────────────────────────────────────────────────────────────

class AdaptiveThresholdRegistry:
    """
    Manages all (signal, station) controllers and persists state to DB.

    FP 6.4 Section 7.2.1: Central registry for threshold state.
    """

    def __init__(self, db_path: str = ''):
        if not db_path:
            repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
            db_path = os.path.join(repo_root, 'data', 'adaptive_thresholds.db')
        self.db_path = db_path
        self.controllers: Dict[Tuple[str, str], AdaptiveThresholdController] = {}
        self._load_state()

    def get_threshold(self, signal_name: str, station: str) -> float:
        """
        Get current adaptive threshold for a signal-station pair.

        Returns the threshold in [0, 1] range. Signals with confidence
        below this threshold should be excluded from the ensemble.
        """
        key = (signal_name, station)
        if key not in self.controllers:
            return self._default_threshold(signal_name)
        return self.controllers[key].current_threshold

    def record_outcome(self, signal_name: str, station: str,
                       calibrated_confidence: float, was_correct: bool) -> None:
        """
        Record a settled outcome and update the controller.

        Args:
            signal_name: Canonical signal name
            station: ICAO station code
            calibrated_confidence: Calibrated P(correct) from calibration pipeline
            was_correct: Whether the prediction was correct
        """
        key = (signal_name, station)
        if key not in self.controllers:
            self._create_controller(signal_name, station)
        self.controllers[key].update(calibrated_confidence, was_correct)

    def filter_signals(self, signals: List[dict]) -> List[dict]:
        """
        Filter a list of signal dicts by adaptive threshold.

        Args:
            signals: List of signal dicts, each with 'type' (or 'signal_name'),
                     'station', and 'confidence' keys.

        Returns:
            Filtered list with 'adaptive_threshold' added to passing signals.
        """
        passed = []
        for s in signals:
            sig_name = s.get('type') or s.get('signal_name', 'unknown')
            station = s.get('station', '')
            confidence = s.get('confidence', 0.0)

            threshold = self.get_threshold(sig_name, station)

            if confidence >= threshold:
                s_copy = dict(s)
                s_copy['adaptive_threshold'] = threshold
                passed.append(s_copy)
            else:
                logger.debug(
                    "Signal %s/%s filtered: conf=%.3f < threshold=%.3f",
                    sig_name, station, confidence, threshold
                )
        return passed

    def get_controller_status(self, signal_name: str, station: str) -> Optional[dict]:
        """Get detailed status for a specific controller."""
        key = (signal_name, station)
        controller = self.controllers.get(key)
        if controller is None:
            return None
        return controller.get_status()

    def all_status(self) -> dict:
        """Get status of all controllers for monitoring."""
        return {
            f"{sig}/{sta}": ctrl.get_status()
            for (sig, sta), ctrl in self.controllers.items()
        }

    def _default_threshold(self, signal_name: str) -> float:
        """Get default threshold for a signal when no data exists."""
        config = SIGNAL_CONFIGS.get(signal_name, DEFAULT_CONFIG)
        return config.tau_min + 0.1  # Conservative start

    def _create_controller(self, signal_name: str, station: str) -> None:
        """Create a new controller for a (signal, station) pair."""
        config = SIGNAL_CONFIGS.get(signal_name, DEFAULT_CONFIG)
        self.controllers[(signal_name, station)] = AdaptiveThresholdController(
            signal_name, station, config
        )

    def _load_state(self) -> None:
        """Load persistent state from SQLite DB."""
        if not os.path.exists(self.db_path):
            logger.debug("No existing state DB at %s, starting fresh", self.db_path)
            return
        try:
            with sqlite3.connect(self.db_path, timeout=5) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT signal_name, station, prior_alpha, prior_beta, "
                    "fast_ema, slow_ema, current_threshold, total_observations, "
                    "correct_observations FROM thresholds"
                ).fetchall()
                for row in rows:
                    config = SIGNAL_CONFIGS.get(row['signal_name'], DEFAULT_CONFIG)
                    controller = AdaptiveThresholdController(
                        row['signal_name'], row['station'], config,
                        prior_alpha=row['prior_alpha'],
                        prior_beta=row['prior_beta'],
                    )
                    controller.fast_ema = row['fast_ema']
                    controller.slow_ema = row['slow_ema']
                    controller.current_threshold = row['current_threshold']
                    controller.total_observations = row['total_observations']
                    controller.correct_observations = row['correct_observations']
                    self.controllers[(row['signal_name'], row['station'])] = controller
            logger.info("Loaded %d threshold controllers from %s", len(rows), self.db_path)
        except (sqlite3.Error, OSError) as e:
            logger.warning("Failed to load threshold state: %s", e)

    def _persist_state(self) -> None:
        """Persist current state to SQLite DB."""
        try:
            os.makedirs(os.path.dirname(self.db_path) or '.', exist_ok=True)
            conn = sqlite3.connect(self.db_path, timeout=5)
            conn.execute("PRAGMA journal_mode=WAL;")

            # Create table if not exists
            conn.execute("""
                CREATE TABLE IF NOT EXISTS thresholds (
                    signal_name TEXT NOT NULL,
                    station TEXT NOT NULL,
                    prior_alpha REAL NOT NULL,
                    prior_beta REAL NOT NULL,
                    fast_ema REAL NOT NULL,
                    slow_ema REAL NOT NULL,
                    current_threshold REAL NOT NULL,
                    total_observations INTEGER NOT NULL DEFAULT 0,
                    correct_observations INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT DEFAULT (datetime('now')),
                    PRIMARY KEY (signal_name, station)
                )
            """)

            # Upsert all controllers
            for (sig, sta), ctrl in self.controllers.items():
                conn.execute("""
                    INSERT OR REPLACE INTO thresholds
                    (signal_name, station, prior_alpha, prior_beta,
                     fast_ema, slow_ema, current_threshold,
                     total_observations, correct_observations, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                """, (
                    sig, sta,
                    ctrl.prior_alpha, ctrl.prior_beta,
                    ctrl.fast_ema, ctrl.slow_ema,
                    ctrl.current_threshold,
                    ctrl.total_observations, ctrl.correct_observations,
                ))

            conn.commit()
            conn.close()
            logger.debug("Persisted %d threshold controllers", len(self.controllers))
        except (sqlite3.Error, OSError) as e:
            logger.warning("Failed to persist threshold state: %s", e)


# ─────────────────────────────────────────────────────────────────────
# Module-level singleton for convenience
# ─────────────────────────────────────────────────────────────────────

_registry: Optional[AdaptiveThresholdRegistry] = None


def get_registry() -> AdaptiveThresholdRegistry:
    """Get the module-level singleton registry."""
    global _registry
    if _registry is None:
        _registry = AdaptiveThresholdRegistry()
    return _registry


# Legacy compatibility — maps to new API
def get_adaptive_threshold(signal_name: str, station: str) -> float:
    """Legacy: get adaptive threshold for a signal-station pair."""
    return get_registry().get_threshold(signal_name, station)


def record_outcome(signal_name: str, station: str,
                   calibrated_confidence: float, was_correct: bool) -> None:
    """Legacy: record a settled outcome."""
    get_registry().record_outcome(signal_name, station, calibrated_confidence, was_correct)


def filter_signals(signals: List[dict]) -> List[dict]:
    """Legacy: filter signals by adaptive threshold."""
    return get_registry().filter_signals(signals)