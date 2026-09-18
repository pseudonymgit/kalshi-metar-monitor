#!/usr/bin/env python3
"""
Degradation Monitor — Rolling Accuracy Monitor + Calibration Drift Detection

Minimum viable degradation detection system for the weather engine trading
signal. Tracks two core promises:

1. **Signal promise**: Is directional prediction accuracy maintained over time?
2. **Calibration promise**: Is confidence calibration reliable?

Three detection mechanisms:
- **EWMA + SPRT**: Exponentially weighted moving average accuracy with
  sequential probability ratio test for formal statistical decision boundaries.
- **CUSUM**: Cumulative sum on residuals to detect regime transitions as they
  happen (not after).
- **Rolling ECE**: Expected calibration error computed over a sliding EWMA
  window, with tail analysis for overconfidence detection.

Design per DEGRADATION-DETECTION-SPEC.md:
- Alpha = 0.01 → effective half-life ≈ 69 trades
- SPRT: H₀ accuracy ≥ 55% (profitable) vs H₁ accuracy ≤ 50% (chance)
- CUSUM: Upper/lower with drift allowance 0.02, threshold h = 5
- ECE: 10 bins, EWMA alpha = 0.02, alert at 0.05/0.10/0.20

B-Mode compliance: No AI/ML models. Pure numpy EWMA, SPRT log-likelihood,
CUSUM accumulator, and ECE arithmetic.

Usage (see end of file for self-test):
    from core.degradation_monitor import DegradationMonitor

    monitor = DegradationMonitor()
    monitor.record_trade("KNYC", "up", 0.75, was_correct=True)
    monitor.record_trade("KNYC", "down", 0.80, was_correct=False)
    status = monitor.get_status()
    report = monitor.generate_report()
"""

import json
import logging
import math
import os
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

_LOGGER = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────

# EWMA: effective half-life ≈ ln(2)/alpha trades
DEFAULT_EWMA_ALPHA_SHORT = 0.01    # half-life ≈ 69 trades (~3-4 weeks)
DEFAULT_EWMA_ALPHA_MEDIUM = 0.005  # half-life ≈ 138 trades (~7 weeks)
DEFAULT_EWMA_ALPHA_LONG = 0.002    # half-life ≈ 346 trades (~larger window)

# SPRT parameters
SPRT_P0 = 0.55  # H₀ boundary (profitable threshold)
SPRT_P1 = 0.50  # H₁ boundary (chance)
SPRT_ALPHA = 0.05  # False positive rate
SPRT_BETA = 0.20   # False negative rate

# CUSUM parameters
CUSUM_DRIFT = 0.02   # Allowable drift per trade
CUSUM_THRESHOLD = 5.0  # Alert threshold

# Rolling ECE
ECE_BIN_EDGES = np.array([0.50 + i * 0.05 for i in range(10)] + [1.00])
ECE_N_BINS = 10
ECE_EWMA_ALPHA = 0.02  # Rolling ECE smoothing

# MDE thresholds (Minimum Detectable Effect)
# YELLOW: accuracy < 55% (lowest profitable)
# ORANGE: accuracy < 50% (chance)
# RED: accuracy < 45% (adverse selection)
THRESHOLD_YELLOW = 0.55
THRESHOLD_ORANGE = 0.50
THRESHOLD_RED = 0.45

# ECE thresholds
ECE_YELLOW = 0.05
ECE_ORANGE = 0.10
ECE_RED = 0.20

# Drift detection (from platt_calibration)
DRIFT_ALPHA_THRESHOLD = 0.5
DRIFT_BETA_THRESHOLD = 0.3

# Grace period after recalibration (trades)
GRACE_PERIOD_TRADES = 5

# Cache file for persistence
DEFAULT_CACHE_FILE = "data/degradation_monitor_cache.json"

# Report directory
REPORT_DIR = "reports"

# Station list (20 stations from climate groups)
ALL_STATIONS = [
    "KPHX", "KLAS", "KAUS", "KSAT",  # desert
    "KLAX", "KSFO", "KSEA",          # coastal_warm
    "KHOU", "KMIA", "KMSY", "KATL",  # coastal_humid
    "KNYC", "KBOS", "KDCA", "KPHL", "KMDW",  # continental
    "KDEN", "KDFW", "KOKC", "KMSP",  # interior
]

DIRECTIONS = ["up", "down"]
AGGR_LAYERS = ["global", "station", "direction"]


# ──────────────────────────────────────────────
# SPRT Calculator
# ──────────────────────────────────────────────

class SPRTCalculator:
    """
    Sequential Probability Ratio Test for accuracy monitoring.

    Tests H₀: p ≥ p0 (profitable) vs H₁: p ≤ p1 (chance or worse).

    After each trade, computes the log-likelihood ratio and decides:
    - continue: need more data
    - reject H₀ (accuracy too low → ALERT)
    - accept H₀ (accuracy maintained → CLEAR)

    B-Mode: Pure arithmetic, no AI/ML.
    """

    def __init__(
        self,
        p0: float = SPRT_P0,
        p1: float = SPRT_P1,
        alpha: float = SPRT_ALPHA,
        beta: float = SPRT_BETA,
    ):
        self.p0 = p0
        self.p1 = p1
        self.p_mid = (p0 + p1) / 2.0  # Operating point for SPRT

        # Log-likelihood ratios for correct/wrong under each hypothesis
        self._llr_correct = math.log(self.p_mid / p0)
        self._llr_wrong = math.log((1.0 - self.p_mid) / (1.0 - p0))

        # Decision boundaries
        self._upper_bound = math.log((1.0 - beta) / alpha)    # Accept H₁
        self._lower_bound = math.log(beta / (1.0 - alpha))    # Accept H₀

        self.log_likelihood_ratio = 0.0
        self.trials = 0
        self.correct_count = 0
        self._decision = "continue"  # continue | alert | clear

    def record(self, was_correct: bool) -> str:
        """
        Record one trade outcome and update SPRT.

        Args:
            was_correct: True if trade was directionally correct.

        Returns:
            Decision string: 'continue', 'alert', or 'clear'.
        """
        self.trials += 1
        if was_correct:
            self.correct_count += 1
            self.log_likelihood_ratio += self._llr_correct
        else:
            self.log_likelihood_ratio += self._llr_wrong

        # Check boundaries
        if self.log_likelihood_ratio >= self._upper_bound:
            self._decision = "alert"  # Reject H₀ — accuracy too low
        elif self.log_likelihood_ratio <= self._lower_bound:
            self._decision = "clear"  # Accept H₀ — accuracy maintained
        else:
            self._decision = "continue"

        return self._decision

    def reset(self) -> None:
        """Reset SPRT state."""
        self.log_likelihood_ratio = 0.0
        self.trials = 0
        self.correct_count = 0
        self._decision = "continue"

    @property
    def decision(self) -> str:
        return self._decision

    @property
    def current_accuracy(self) -> float:
        if self.trials == 0:
            return 0.50
        return self.correct_count / self.trials

    def to_dict(self) -> Dict[str, Any]:
        return {
            "log_likelihood_ratio": round(self.log_likelihood_ratio, 4),
            "trials": self.trials,
            "correct_count": self.correct_count,
            "current_accuracy": round(self.current_accuracy, 4),
            "decision": self._decision,
            "upper_bound": round(self._upper_bound, 4),
            "lower_bound": round(self._lower_bound, 4),
            "p0": self.p0,
            "p1": self.p1,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SPRTCalculator":
        cal = cls(p0=d.get("p0", SPRT_P0), p1=d.get("p1", SPRT_P1))
        cal.log_likelihood_ratio = d.get("log_likelihood_ratio", 0.0)
        cal.trials = d.get("trials", 0)
        cal.correct_count = d.get("correct_count", 0)
        cal._decision = d.get("decision", "continue")
        return cal


# ──────────────────────────────────────────────
# CUSUM Detector
# ──────────────────────────────────────────────

class CUSUMDetector:
    """
    Cumulative Sum detector for regime change detection.

    Monitors the residual r_t = correct_t - ewma_accuracy_{t-1}.
    Under stationarity, residuals are mean-zero. A sustained shift in
    accuracy causes the CUSUM to accumulate.

    Upper CUSUM: S_t = max(0, S_{t-1} + r_t - drift_allowance)
    Lower CUSUM: T_t = max(0, T_{t-1} - r_t - drift_allowance)

    B-Mode: Pure arithmetic accumulator.
    """

    def __init__(
        self,
        drift_allowance: float = CUSUM_DRIFT,
        threshold: float = CUSUM_THRESHOLD,
    ):
        self.drift_allowance = drift_allowance
        self.threshold = threshold

        self.upper_cusum = 0.0  # Detects accuracy drops
        self.lower_cusum = 0.0  # Detects accuracy rises
        self._alerted_upper = False
        self._alerted_lower = False

        # Timestamp of transition detection
        self.transition_time: Optional[float] = None
        self.transition_direction: Optional[str] = None  # "drop" or "rise"

    def record(self, residual: float) -> Dict[str, Any]:
        """
        Record one residual and update CUSUM.

        Args:
            residual: r_t = correct_t - ewma_accuracy_{t-1}

        Returns:
            Dict with current CUSUM values and alert state.
        """
        # Upper CUSUM detects accuracy drops: accumulates when residual is negative
        # (actual outcome below expectation). Uses -residual so negative residuals
        # make S_t grow.
        self.upper_cusum = max(0.0, self.upper_cusum - residual - self.drift_allowance)
        # Lower CUSUM detects accuracy rises: accumulates when residual is positive
        # (actual outcome above expectation).
        self.lower_cusum = max(0.0, self.lower_cusum + residual - self.drift_allowance)

        result = {
            "upper_cusum": round(self.upper_cusum, 4),
            "lower_cusum": round(self.lower_cusum, 4),
            "upper_alert": self.upper_cusum > self.threshold,
            "lower_alert": self.lower_cusum > self.threshold,
        }

        # Alert on first crossing (not persistent — one-shot detection)
        if self.upper_cusum > self.threshold and not self._alerted_upper:
            self._alerted_upper = True
            self.transition_time = time.time()
            self.transition_direction = "drop"
            result["transition_detected"] = True
            result["transition_direction"] = "drop"

        if self.lower_cusum > self.threshold and not self._alerted_lower:
            self._alerted_lower = True
            self.transition_time = time.time()
            self.transition_direction = "rise"
            result["transition_detected"] = True
            result["transition_direction"] = "rise"

        return result

    def reset(self) -> None:
        """Reset CUSUM state."""
        self.upper_cusum = 0.0
        self.lower_cusum = 0.0
        self._alerted_upper = False
        self._alerted_lower = False
        self.transition_time = None
        self.transition_direction = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "upper_cusum": round(self.upper_cusum, 4),
            "lower_cusum": round(self.lower_cusum, 4),
            "threshold": self.threshold,
            "drift_allowance": self.drift_allowance,
            "alerted_upper": self._alerted_upper,
            "alerted_lower": self._alerted_lower,
            "transition_direction": self.transition_direction,
            "transition_time_iso": (
                datetime.fromtimestamp(self.transition_time, tz=timezone.utc).isoformat()
                if self.transition_time else None
            ),
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "CUSUMDetector":
        det = cls(
            drift_allowance=d.get("drift_allowance", CUSUM_DRIFT),
            threshold=d.get("threshold", CUSUM_THRESHOLD),
        )
        det.upper_cusum = d.get("upper_cusum", 0.0)
        det.lower_cusum = d.get("lower_cusum", 0.0)
        det._alerted_upper = d.get("alerted_upper", False)
        det._alerted_lower = d.get("alerted_lower", False)
        ts = d.get("transition_time")
        det.transition_time = ts
        det.transition_direction = d.get("transition_direction")
        return det


# ──────────────────────────────────────────────
# Rolling EWMA Accuracy Tracker
# ──────────────────────────────────────────────

class EWMATracker:
    """
    Per-cell exponentially weighted moving average accuracy.

    accuracy_t = alpha * correct_t + (1 - alpha) * accuracy_{t-1}

    Manages three window sizes:
    - Short (α=0.01):  ~69 trade half-life (~30-day window)
    - Medium (α=0.005): ~138 trade half-life (~90-day window)
    - Long (α=0.002):   ~346 trade half-life (~365-day window)

    B-Mode: Pure EWMA arithmetic, no AI/ML.
    """

    def __init__(
        self,
        alpha_short: float = DEFAULT_EWMA_ALPHA_SHORT,
        alpha_medium: float = DEFAULT_EWMA_ALPHA_MEDIUM,
        alpha_long: float = DEFAULT_EWMA_ALPHA_LONG,
    ):
        self.alpha_short = alpha_short
        self.alpha_medium = alpha_medium
        self.alpha_long = alpha_long

        # EWMA state per window
        self.accuracy_short = 0.50  # Prior: 50/50
        self.accuracy_medium = 0.50
        self.accuracy_long = 0.50

        self.n = 0  # Total trades observed
        self.n_correct = 0
        self.last_timestamp: Optional[float] = None

    def record(self, was_correct: bool) -> None:
        """Record one trade outcome and update all EWMA windows."""
        correct_val = 1.0 if was_correct else 0.0

        self.accuracy_short = (
            self.alpha_short * correct_val
            + (1.0 - self.alpha_short) * self.accuracy_short
        )
        self.accuracy_medium = (
            self.alpha_medium * correct_val
            + (1.0 - self.alpha_medium) * self.accuracy_medium
        )
        self.accuracy_long = (
            self.alpha_long * correct_val
            + (1.0 - self.alpha_long) * self.accuracy_long
        )

        self.n += 1
        if was_correct:
            self.n_correct += 1
        self.last_timestamp = time.time()

    def get_alert_level(self) -> Optional[str]:
        """
        Compare short-window EWMA against long-window.

        Alert if short-window accuracy drops more than 2σ below
        long-window accuracy.

        Returns:
            None (normal), 'YELLOW', 'ORANGE', or 'RED'.
        """
        if self.n < 20:
            return None  # Not enough data

        # 2σ threshold: approximate standard deviation of EWMA
        # EWMA variance ≈ alpha / (2 - alpha) * p * (1-p) / ?
        # Simplified: 2σ ≈ 0.10 for alpha=0.01, n>100
        sigma_approx = math.sqrt(
            (self.alpha_short / (2.0 - self.alpha_short))
            * (self.accuracy_long * (1.0 - self.accuracy_long))
            / max(1, self.n)
        )

        short_vs_long_delta = self.accuracy_long - self.accuracy_short
        two_sigma_threshold = 2.0 * sigma_approx

        # Check direct thresholds first
        if self.accuracy_short < THRESHOLD_RED:
            return "RED"
        elif self.accuracy_short < THRESHOLD_ORANGE:
            return "ORANGE"
        elif self.accuracy_short < THRESHOLD_YELLOW:
            return "YELLOW"

        # Relative decline from long-term baseline
        if self.n > 50 and short_vs_long_delta > two_sigma_threshold:
            return "YELLOW"

        return None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "accuracy_30d_ewma": round(self.accuracy_short, 4),
            "accuracy_90d_ewma": round(self.accuracy_medium, 4),
            "accuracy_365d_ewma": round(self.accuracy_long, 4),
            "n_total": self.n,
            "n_correct": self.n_correct,
            "n_accuracy": round(self.n_correct / max(1, self.n), 4),
            "alert_level": self.get_alert_level(),
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "EWMATracker":
        t = cls(
            alpha_short=d.get("alpha_short", DEFAULT_EWMA_ALPHA_SHORT),
            alpha_medium=d.get("alpha_medium", DEFAULT_EWMA_ALPHA_MEDIUM),
            alpha_long=d.get("alpha_long", DEFAULT_EWMA_ALPHA_LONG),
        )
        t.accuracy_short = d.get("accuracy_30d_ewma", 0.50)
        t.accuracy_medium = d.get("accuracy_90d_ewma", 0.50)
        t.accuracy_long = d.get("accuracy_365d_ewma", 0.50)
        t.n = d.get("n_total", 0)
        t.n_correct = d.get("n_correct", 0)
        return t


# ──────────────────────────────────────────────
# Rolling ECE Calculator
# ──────────────────────────────────────────────

class RollingECE:
    """
    Rolling Expected Calibration Error using EWMA per bin.

    Maintains 10 confidence bins [50-55%, 55-60%, ..., 95-100%].
    For each bin, tracks:
    - EWMA of observed accuracy
    - EWMA-weighted count (effective sample size)

    ECE = sum(bin_weight_i * |ewma_accuracy_i - bin_midpoint_i|)

    Also tracks tail calibration (p < 0.2, p > 0.8) for overconfidence
    and underconfidence detection.

    B-Mode: Pure arithmetic per-bin EWMA, no AI/ML.
    """

    def __init__(self, alpha: float = ECE_EWMA_ALPHA):
        self.alpha = alpha
        self.n_bins = ECE_N_BINS

        # Per-bin EWMA state: (ewma_accuracy, ewma_count)
        self._bin_accuracy: List[float] = [0.50] * self.n_bins
        self._bin_count: List[float] = [0.0] * self.n_bins  # Effective count

        # Total trades observed
        self.n_total = 0

        # Last computed ECE
        self.last_ece: float = 0.0
        self.last_bin_details: List[Dict[str, Any]] = []

        # Tail-specific metrics
        self.tail_low_avg_conf: Optional[float] = None
        self.tail_low_avg_accuracy: Optional[float] = None
        self.tail_high_avg_conf: Optional[float] = None
        self.tail_high_avg_accuracy: Optional[float] = None

    def record(self, calibrated_conf: float, was_correct: bool) -> Dict[str, Any]:
        """
        Record one trade with its calibrated confidence.

        Args:
            calibrated_conf: Platt-calibrated confidence [0.5, 1.0].
            was_correct: True if trade was directionally correct.

        Returns:
            Dict with ECE value and per-bin details.
        """
        conf = float(np.clip(calibrated_conf, 0.50, 1.0))
        correct = 1.0 if was_correct else 0.0
        self.n_total += 1

        # Find bin
        bin_idx = min(self.n_bins - 1, int(np.searchsorted(ECE_BIN_EDGES, conf, side="right") - 1))
        bin_idx = max(0, bin_idx)

        # Update EWMA
        self._bin_accuracy[bin_idx] = (
            self.alpha * correct + (1.0 - self.alpha) * self._bin_accuracy[bin_idx]
        )
        self._bin_count[bin_idx] = (
            self.alpha * 1.0 + (1.0 - self.alpha) * self._bin_count[bin_idx]
        )

        # Recompute ECE
        self._compute_ece()

        # Recompute tail metrics
        self._compute_tail_metrics()

        return {
            "ece": round(self.last_ece, 6),
            "n_total": self.n_total,
            "alert_level": self._get_alert_level(),
        }

    def _compute_ece(self) -> None:
        """Recompute ECE from per-bin EWMA state."""
        bin_details = []
        total_weight = sum(self._bin_count)

        if total_weight < 1e-9:
            self.last_ece = 0.0
            self.last_bin_details = []
            return

        ece = 0.0
        for i in range(self.n_bins):
            lo = float(ECE_BIN_EDGES[i])
            hi = float(ECE_BIN_EDGES[i + 1])
            mid = (lo + hi) / 2.0

            acc = self._bin_accuracy[i]
            weight = self._bin_count[i]
            bin_weight = weight / total_weight if total_weight > 0 else 0.0

            gap = abs(acc - mid)
            contribution = bin_weight * gap
            ece += contribution

            bin_details.append({
                "bin": f"{lo*100:.0f}-{hi*100:.0f}%",
                "conf_low": round(lo, 2),
                "conf_high": round(hi, 2),
                "midpoint": round(mid, 2),
                "ewma_accuracy": round(acc, 4),
                "ewma_count": round(weight, 2),
                "gap": round(gap, 4),
                "contribution_to_ece": round(contribution, 6),
            })

        self.last_ece = ece
        self.last_bin_details = bin_details

    def _compute_tail_metrics(self) -> None:
        """
        Compute calibration metrics for tail bins:
        - Low tail: p < 0.20 (not applicable for calibrated conf [0.5, 1.0],
          but we track bins 0-2 as "low confidence range")
        - High tail: p > 0.80 (overconfidence detection) — bins 6-9
        """
        # Low confidence tail (bins 0-2: 50-65%)
        low_confs = []
        low_accs = []
        for i in range(min(3, self.n_bins)):
            if self._bin_count[i] > 0.5:  # Meaningful EWMA count
                lo = float(ECE_BIN_EDGES[i])
                hi = float(ECE_BIN_EDGES[i + 1])
                low_confs.append((lo + hi) / 2.0)
                low_accs.append(self._bin_accuracy[i])

        self.tail_low_avg_conf = float(np.mean(low_confs)) if low_confs else None
        self.tail_low_avg_accuracy = float(np.mean(low_accs)) if low_accs else None

        # High confidence tail (bins 6-9: 80-100%)
        high_confs = []
        high_accs = []
        for i in range(6, self.n_bins):
            if self._bin_count[i] > 0.5:
                lo = float(ECE_BIN_EDGES[i])
                hi = float(ECE_BIN_EDGES[i + 1])
                high_confs.append((lo + hi) / 2.0)
                high_accs.append(self._bin_accuracy[i])

        self.tail_high_avg_conf = float(np.mean(high_confs)) if high_confs else None
        self.tail_high_avg_accuracy = float(np.mean(high_accs)) if high_accs else None

    def _get_alert_level(self) -> Optional[str]:
        """Determine ECE alert level."""
        if self.last_ece >= ECE_RED:
            return "RED"
        elif self.last_ece >= ECE_ORANGE:
            return "ORANGE"
        elif self.last_ece >= ECE_YELLOW:
            return "YELLOW"
        return None

    def get_tail_summary(self) -> Dict[str, Any]:
        """
        Summary of tail calibration metrics.

        Returns:
            Dict with low/high tail details, including any over/underconfidence
            flags.
        """
        result: Dict[str, Any] = {
            "low_confidence_tail": {
                "avg_confidence": round(self.tail_low_avg_conf, 4) if self.tail_low_avg_conf is not None else None,
                "avg_accuracy": round(self.tail_low_avg_accuracy, 4) if self.tail_low_avg_accuracy is not None else None,
            },
            "high_confidence_tail": {
                "avg_confidence": round(self.tail_high_avg_conf, 4) if self.tail_high_avg_conf is not None else None,
                "avg_accuracy": round(self.tail_high_avg_accuracy, 4) if self.tail_high_avg_accuracy is not None else None,
            },
        }

        # Flag overconfidence in high tail
        if self.tail_high_avg_conf is not None and self.tail_high_avg_accuracy is not None:
            gap = self.tail_high_avg_conf - self.tail_high_avg_accuracy
            result["high_confidence_tail"]["overconfidence_gap"] = round(gap, 4)
            result["high_confidence_tail"]["overconfidence_flag"] = gap > 0.15  # >15pp gap

        # Flag underconfidence in low tail
        if self.tail_low_avg_accuracy is not None and self.tail_low_avg_conf is not None:
            gap = self.tail_low_avg_accuracy - self.tail_low_avg_conf
            result["low_confidence_tail"]["underconfidence_gap"] = round(gap, 4)
            result["low_confidence_tail"]["underconfidence_flag"] = gap > 0.15

        return result

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ece": round(self.last_ece, 6),
            "ece_alert_level": self._get_alert_level(),
            "n_total": self.n_total,
            "bins": self.last_bin_details,
            "tail_summary": self.get_tail_summary(),
            # Raw arrays for serialization/deserialization
            "bin_accuracy": self._bin_accuracy,
            "bin_count": self._bin_count,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "RollingECE":
        r = cls(alpha=d.get("alpha", ECE_EWMA_ALPHA))
        r._bin_accuracy = d.get("bin_accuracy", [0.50] * r.n_bins)
        r._bin_count = d.get("bin_count", [0.0] * r.n_bins)
        r.n_total = d.get("n_total", 0)
        # Recompute derived state
        r._compute_ece()
        r._compute_tail_metrics()
        return r


# ──────────────────────────────────────────────
# DegradationMonitor — composite monitor
# ──────────────────────────────────────────────

class DegradationMonitor:
    """
    Composite degradation detection monitor.

    Tracks:
    - Global EWMA accuracy (short/medium/long windows)
    - SPRT against profitability threshold
    - CUSUM for regime transition detection
    - Rolling ECE for calibration drift
    - Per-station EWMA accuracy
    - Per-direction EWMA accuracy

    All records are maintained in-memory and persisted to a cache file
    for crash recovery.

    B-Mode compliance: No AI/ML. Pure numpy/scipy EWMA, SPRT, CUSUM, ECE.
    """

    def __init__(
        self,
        cache_file: str = DEFAULT_CACHE_FILE,
        report_dir: str = REPORT_DIR,
    ):
        self.cache_file = Path(cache_file)
        self.report_dir = Path(report_dir)

        # ── Global monitors ──
        self.global_ewma = EWMATracker()
        self.global_sprt = SPRTCalculator()
        self.global_cusum = CUSUMDetector()
        self.global_ece = RollingECE()

        # ── Per-layer monitors ──
        # Station-level: {station: EWMATracker}
        self.station_ewma: Dict[str, EWMATracker] = {}
        self.station_sprt: Dict[str, SPRTCalculator] = {}

        # Direction-level: {direction: EWMATracker}
        self.direction_ewma: Dict[str, EWMATracker] = {}

        # ── State ──
        self._last_recalibration_time: Optional[float] = None
        self._trades_since_recalibration = 0
        self._last_alert_time: Optional[float] = None
        self._last_alert_level: Optional[str] = None
        self._last_alert_message: Optional[str] = None
        self._alert_history: List[Dict[str, Any]] = []
        self._max_alert_history = 200

        # ── Prior calibration state (for check_drift integration) ──
        self._prior_calibration_state: Optional[Dict[str, Any]] = None

        # Load from cache
        self._load_cache()

    # ── Public API ──────────────────────────────────────────────

    def record_trade(
        self,
        station: str,
        direction: str,
        calibrated_conf: float,
        was_correct: bool,
    ) -> Dict[str, Any]:
        """
        Record one completed trade and update all monitoring layers.

        Args:
            station: ICAO station code.
            direction: 'up' or 'down'.
            calibrated_conf: Platt-calibrated confidence [0.5, 1.0].
            was_correct: True if trade was directionally correct.

        Returns:
            Dict with status updates from all layers.
        """
        station = station.upper()
        direction = direction.lower()
        correct_f = 1.0 if was_correct else 0.0

        # ── Update global EWMA ──
        self.global_ewma.record(was_correct)

        # ── Update global SPRT ──
        sprt_decision = self.global_sprt.record(was_correct)

        # ── Update global CUSUM ──
        residual = correct_f - self.global_ewma.accuracy_short
        cusum_result = self.global_cusum.record(residual)

        # ── Update global ECE ──
        ece_result = self.global_ece.record(calibrated_conf, was_correct)

        # ── Update station-level ──
        if station not in self.station_ewma:
            self.station_ewma[station] = EWMATracker()
            self.station_sprt[station] = SPRTCalculator()
        self.station_ewma[station].record(was_correct)
        self.station_sprt[station].record(was_correct)

        # ── Update direction-level ──
        if direction not in self.direction_ewma:
            self.direction_ewma[direction] = EWMATracker()
        self.direction_ewma[direction].record(was_correct)

        # ── Post-recalibration grace period ──
        if self._last_recalibration_time is not None:
            self._trades_since_recalibration += 1

        # ── Check alert conditions ──
        alert_info = self._check_alerts()

        # ── Persist periodically ──
        if self.global_ewma.n % 10 == 0:
            self._save_cache()

        return {
            "sprt_decision": sprt_decision,
            "cusum": cusum_result,
            "ece": ece_result,
            "alert": alert_info,
        }


    def get_status(self, layer: str = "all") -> Dict[str, Any]:
        """
        Get current monitoring status for one or all layers.

        Args:
            layer: 'global', 'station', 'direction', or 'all'.

        Returns:
            Dict with status for requested layers.
        """
        result: Dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "monitor_version": "1.0",
        }

        if layer in ("global", "all"):
            result["global"] = {
                "ewma": self.global_ewma.to_dict(),
                "sprt": self.global_sprt.to_dict(),
                "cusum": self.global_cusum.to_dict(),
                "ece": self.global_ece.to_dict(),
            }

        if layer in ("station", "all"):
            station_status = {}
            for station in sorted(self.station_ewma.keys()):
                station_status[station] = {
                    "ewma": self.station_ewma[station].to_dict(),
                    "sprt": self.station_sprt.get(station, SPRTCalculator()).to_dict(),
                }
            result["stations"] = station_status

        if layer in ("direction", "all"):
            dir_status = {}
            for direction in sorted(self.direction_ewma.keys()):
                dir_status[direction] = self.direction_ewma[direction].to_dict()
            result["directions"] = dir_status

        result["state"] = {
            "trades_since_recalibration": self._trades_since_recalibration,
            "last_alert_level": self._last_alert_level,
            "last_alert_time_iso": (
                datetime.fromtimestamp(self._last_alert_time, tz=timezone.utc).isoformat()
                if self._last_alert_time else None
            ),
            "alert_history_count": len(self._alert_history),
        }

        return result

    def generate_report(self) -> Dict[str, Any]:
        """
        Generate a comprehensive degradation monitoring report.

        Returns:
            Dict with full system status, suitable for JSON serialization.
        """
        report = self.get_status("all")

        # Add alert history
        report["alert_history"] = self._alert_history[-50:]  # Last 50 alerts

        # Add per-layer worst stations
        worst_stations = sorted(
            [
                (s, t.accuracy_short)
                for s, t in self.station_ewma.items()
            ],
            key=lambda x: x[1],
        )[:5]
        report["worst_stations"] = [
            {"station": s, "accuracy_30d_ewma": round(a, 4)}
            for s, a in worst_stations
        ]

        # Add calibration integration status
        report["calibration_integration"] = {
            "check_drift_enabled": self._prior_calibration_state is not None,
            "recalibration_available": self._last_recalibration_time is not None,
            "trades_since_recalibration": self._trades_since_recalibration,
            "grace_period_active": (
                self._trades_since_recalibration < GRACE_PERIOD_TRADES
                if self._last_recalibration_time is not None
                else False
            ),
        }

        report["thresholds"] = {
            "accuracy_yellow": THRESHOLD_YELLOW,
            "accuracy_orange": THRESHOLD_ORANGE,
            "accuracy_red": THRESHOLD_RED,
            "ece_yellow": ECE_YELLOW,
            "ece_orange": ECE_ORANGE,
            "ece_red": ECE_RED,
            "sprt_p0_h0": SPRT_P0,
            "sprt_p1_h1": SPRT_P1,
            "cusum_threshold": CUSUM_THRESHOLD,
        }

        return report

    # ── Calibration Integration ──────────────────────────────────

    def set_prior_calibration(self, calibration_state: Dict[str, Any]) -> None:
        """
        Set the prior calibration state for drift comparison.

        Call this after PlattCalibrationPipeline.save() to establish
        a baseline for check_drift.

        Args:
            calibration_state: Output from PlattCalibrationPipeline.to_json_dict()
        """
        self._prior_calibration_state = calibration_state
        self._save_cache()

    def check_calibration_drift(
        self,
        current_calibration_state: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Compare current calibration state against prior.

        This wraps PlattCalibrationPipeline.check_drift() logic.

        Args:
            current_calibration_state: Current state from to_json_dict()

        Returns:
            Dict with drift analysis, or a note if no prior state set.
        """
        if self._prior_calibration_state is None:
            return {
                "drifted_cells": [],
                "total_cells": 0,
                "drift_pct": 0.0,
                "note": "no prior calibration state set — call set_prior_calibration()",
            }

        drifted = []
        prior_cals = {}
        prior_cals.update(self._prior_calibration_state.get("calibrators", {}))
        prior_cals.update(self._prior_calibration_state.get("_fallback", {}))
        prior_cals.update(self._prior_calibration_state.get("_regime_calibrators", {}))
        prior_cals.update(self._prior_calibration_state.get("_season_calibrators", {}))

        current_cals = {}
        current_cals.update(current_calibration_state.get("calibrators", {}))
        current_cals.update(current_calibration_state.get("_fallback", {}))
        current_cals.update(current_calibration_state.get("_regime_calibrators", {}))
        current_cals.update(current_calibration_state.get("_season_calibrators", {}))

        for key, cur_entry in current_cals.items():
            pri_entry = prior_cals.get(key)
            if pri_entry is None:
                continue

            cur_alpha = cur_entry.get("alpha", 1.0)
            cur_beta = cur_entry.get("beta", 0.0)
            pri_alpha = pri_entry.get("alpha", 1.0)
            pri_beta = pri_entry.get("beta", 0.0)

            alpha_change = abs(cur_alpha - pri_alpha)
            beta_change = abs(cur_beta - pri_beta)

            if alpha_change > DRIFT_ALPHA_THRESHOLD or beta_change > DRIFT_BETA_THRESHOLD:
                drifted.append({
                    "key": key,
                    "alpha_change": round(alpha_change, 4),
                    "beta_change": round(beta_change, 4),
                    "prior_alpha": round(pri_alpha, 4),
                    "prior_beta": round(pri_beta, 4),
                    "current_alpha": round(cur_alpha, 4),
                    "current_beta": round(cur_beta, 4),
                })

        total = len(current_cals)
        drift_result = {
            "drifted_cells": drifted,
            "total_cells": total,
            "drifted_count": len(drifted),
            "drift_pct": round(len(drifted) / max(1, total) * 100, 2),
        }

        if len(drifted) > 0:
            _LOGGER.warning(
                "Calibration drift detected",
                extra={
                    "event_id": "calibration_drift",
                    "drifted_count": len(drifted),
                    "drift_pct": drift_result["drift_pct"],
                    "total_cells": total,
                },
            )

        return drift_result

    def mark_recalibration(self) -> None:
        """Mark that a recalibration has been performed (resets grace period)."""
        self._last_recalibration_time = time.time()
        self._trades_since_recalibration = 0

        # Reset SPRT and CUSUM on recalibration
        self.global_sprt.reset()
        self.global_cusum.reset()

        self._save_cache()

    # ── Persistence ──────────────────────────────────────────────

    def _check_alerts(self):
        """Internal: check all alert conditions."""
        alerts_triggered = {}
        now = time.time()

        # EWMA accuracy check
        ewma_alert = self.global_ewma.get_alert_level()
        if ewma_alert is not None:
            alerts_triggered["accuracy"] = {
                "level": ewma_alert,
                "value": round(self.global_ewma.accuracy_short, 4),
            }

        # SPRT check
        if self.global_sprt.decision == "alert" and self.global_sprt.trials >= 20:
            alerts_triggered["sprt"] = {
                "level": "ORANGE",
                "llr": round(self.global_sprt.log_likelihood_ratio, 2),
                "trials": self.global_sprt.trials,
            }

        # ECE check
        ece_alert = self.global_ece._get_alert_level()
        if ece_alert is not None and self.global_ece.n_total >= 20:
            alerts_triggered["ece"] = {
                "level": ece_alert,
                "value": round(self.global_ece.last_ece, 4),
            }

        # CUSUM transition check
        transition_dir = self.global_cusum.transition_direction
        if transition_dir is not None:
            alerts_triggered["cusum_transition"] = {
                "direction": transition_dir,
                "upper": round(self.global_cusum.upper_cusum, 2),
                "lower": round(self.global_cusum.lower_cusum, 2),
            }

        if alerts_triggered:
            self._last_alert_time = now
            self._last_alert_message = str(alerts_triggered)
            self._alert_history.append({
                "timestamp_iso": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
                "alerts": alerts_triggered,
            })
            if len(self._alert_history) > self._max_alert_history:
                self._alert_history = self._alert_history[-self._max_alert_history:]

            levels = {"YELLOW": 1, "ORANGE": 2, "RED": 3}
            max_level = None
            max_val = 0
            for key, info in alerts_triggered.items():
                lvl = info.get("level", "YELLOW")
                val = levels.get(lvl, 0)
                if val > max_val:
                    max_val = val
                    max_level = lvl
            self._last_alert_level = max_level

            _LOGGER.warning(
                "Degradation alert triggered",
                extra={
                    "event_id": "degradation_alert",
                    "alert_level": max_level,
                    "alerts": alerts_triggered,
                    "global_accuracy_30d": round(self.global_ewma.accuracy_short, 4),
                    "global_ece": round(self.global_ece.last_ece, 6),
                },
            )

        return alerts_triggered

    def _save_cache(self) -> None:
        """Save monitor state to cache file."""
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)

            # Serialize station monitors
            station_ewma_dict = {
                s: t.to_dict() for s, t in self.station_ewma.items()
            }
            station_sprt_dict = {
                s: t.to_dict() for s, t in self.station_sprt.items()
            }
            direction_ewma_dict = {
                d: t.to_dict() for d, t in self.direction_ewma.items()
            }

            cache = {
                "version": 1,
                "global_ewma": self.global_ewma.to_dict(),
                "global_sprt": self.global_sprt.to_dict(),
                "global_cusum": self.global_cusum.to_dict(),
                "global_ece": self.global_ece.to_dict(),
                "station_ewma": station_ewma_dict,
                "station_sprt": station_sprt_dict,
                "direction_ewma": direction_ewma_dict,
                "state": {
                    "last_recalibration_time": self._last_recalibration_time,
                    "trades_since_recalibration": self._trades_since_recalibration,
                    "last_alert_time": self._last_alert_time,
                    "last_alert_level": self._last_alert_level,
                    "last_alert_message": self._last_alert_message,
                    "alert_history": self._alert_history[-200:],
                    "prior_calibration_saved": self._prior_calibration_state is not None,
                },
                "saved_at": time.time(),
            }

            with open(self.cache_file, "w") as f:
                json.dump(cache, f, indent=2)

        except Exception as e:
            _LOGGER.warning(f"Failed to save degradation monitor cache: {e}")

    def _load_cache(self) -> None:
        """Load monitor state from cache file."""
        try:
            if not self.cache_file.exists():
                return

            with open(self.cache_file) as f:
                cache = json.load(f)

            version = cache.get("version", 0)
            if version < 1:
                return

            # Restore global monitors
            if "global_ewma" in cache:
                self.global_ewma = EWMATracker.from_dict(cache["global_ewma"])
            if "global_sprt" in cache:
                self.global_sprt = SPRTCalculator.from_dict(cache["global_sprt"])
            if "global_cusum" in cache:
                self.global_cusum = CUSUMDetector.from_dict(cache["global_cusum"])
            if "global_ece" in cache:
                self.global_ece = RollingECE.from_dict(cache["global_ece"])

            # Restore station monitors
            for station, data in cache.get("station_ewma", {}).items():
                self.station_ewma[station] = EWMATracker.from_dict(data)
            for station, data in cache.get("station_sprt", {}).items():
                self.station_sprt[station] = SPRTCalculator.from_dict(data)

            # Restore direction monitors
            for direction, data in cache.get("direction_ewma", {}).items():
                self.direction_ewma[direction] = EWMATracker.from_dict(data)

            # Restore state
            state = cache.get("state", {})
            self._last_recalibration_time = state.get("last_recalibration_time")
            self._trades_since_recalibration = state.get("trades_since_recalibration", 0)
            self._last_alert_time = state.get("last_alert_time")
            self._last_alert_level = state.get("last_alert_level")
            self._last_alert_message = state.get("last_alert_message")
            self._alert_history = state.get("alert_history", [])

            _LOGGER.info(
                "Loaded degradation monitor cache",
                extra={
                    "event_id": "degradation_cache_load",
                    "n_global": self.global_ewma.n,
                    "n_stations": len(self.station_ewma),
                },
            )

        except Exception as e:
            _LOGGER.warning(f"Failed to load degradation monitor cache: {e}")

    def save_report(self, path: Optional[str] = None) -> str:
        """
        Generate and save a monitoring report to disk.

        Args:
            path: Output path (default: reports/degradation_report_{date}.json)

        Returns:
            Path to saved report.
        """
        report = self.generate_report()
        if path is None:
            self.report_dir.mkdir(parents=True, exist_ok=True)
            date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
            path = str(self.report_dir / f"degradation_report_{date_str}.json")

        with open(path, "w") as f:
            json.dump(report, f, indent=2)

        _LOGGER.info(
            "Saved degradation report",
            extra={
                "event_id": "degradation_report_saved",
                "path": path,
                "alert_level": self._last_alert_level,
            },
        )
        return path


# ──────────────────────────────────────────────
# Self-Test
# ──────────────────────────────────────────────

def _self_test():
    """Run basic validation of the degradation monitor modules."""
    print("=" * 60)
    print("Degradation Monitor Self-Test")
    print("=" * 60)

    # ── Test 1: EWMATracker ──
    print("\n--- Test 1: EWMATracker ---")
    ewma = EWMATracker(alpha_short=0.1, alpha_medium=0.05, alpha_long=0.02)
    assert abs(ewma.accuracy_short - 0.50) < 0.001, "Initial EWMA should be 0.50"

    # Record 10 correct trades
    for i in range(10):
        ewma.record(True)
    assert ewma.n == 10
    assert ewma.n_correct == 10
    assert ewma.accuracy_short > 0.50, "10 correct should push accuracy up"
    print(f"  Test 1a PASS: accuracy_short={ewma.accuracy_short:.4f}, n={ewma.n}")

    # Record 10 wrong trades
    for i in range(10):
        ewma.record(False)
    assert ewma.n == 20
    assert ewma.accuracy_short < 0.50, "10 wrong should push accuracy down"
    print(f"  Test 1b PASS: accuracy_short={ewma.accuracy_short:.4f}, n={ewma.n}")

    # Test to_dict / from_dict round-trip
    d = ewma.to_dict()
    ewma2 = EWMATracker.from_dict(d)
    assert abs(ewma2.accuracy_short - ewma.accuracy_short) < 0.0001
    assert ewma2.n == ewma.n
    print("  Test 1c PASS: serialization round-trip")

    # Test alert levels
    assert ewma.get_alert_level() is not None
    print(f"  Test 1d PASS: alert_level={ewma.get_alert_level()}")

    # ── Test 2: SPRTCalculator ──
    print("\n--- Test 2: SPRTCalculator ---")
    sprt = SPRTCalculator()
    assert sprt.decision == "continue"

    # 40 trades at 50% accuracy should trigger alert
    for i in range(40):
        sprt.record(was_correct=(i < 20))
    assert sprt.trials == 40
    assert abs(sprt.current_accuracy - 0.50) < 0.01
    print(f"  Test 2a PASS: trials={sprt.trials}, decision={sprt.decision}, llr={sprt.log_likelihood_ratio:.2f}")

    # Serialization round-trip
    d2 = sprt.to_dict()
    sprt2 = SPRTCalculator.from_dict(d2)
    assert sprt2.trials == sprt.trials
    assert abs(sprt2.log_likelihood_ratio - sprt.log_likelihood_ratio) < 0.0001
    print("  Test 2b PASS: serialization round-trip")

    # Reset should work
    sprt.reset()
    assert sprt.trials == 0
    assert sprt.decision == "continue"
    print("  Test 2c PASS: reset")

    # ── Test 3: CUSUMDetector ──
    print("\n--- Test 3: CUSUMDetector ---")
    cusum = CUSUMDetector(drift_allowance=0.02, threshold=3.0)

    # Series of residuals that should trigger upper CUSUM (accuracy drop)
    for _ in range(100):
        res = -0.3  # Persistent negative residuals
        cusum.record(res)

    assert cusum.transition_direction is not None
    print(f"  Test 3a PASS: transition_direction={cusum.transition_direction}, upper={cusum.upper_cusum:.2f}")

    cusum.reset()
    assert cusum.upper_cusum == 0.0
    assert cusum.transition_direction is None
    print("  Test 3b PASS: reset")

    # ── Test 4: RollingECE ──
    print("\n--- Test 4: RollingECE ---")
    ece = RollingECE(alpha=0.1)

    # Record 100 trades with 80% confidence — 80% accuracy (perfectly calibrated)
    # Use random interleaving so EWMA doesn't get biased by order
    ece_outcomes = [True] * 80 + [False] * 20
    import random as _r
    _r.seed(42)
    _r.shuffle(ece_outcomes)
    for outcome in ece_outcomes:
        ece.record(0.80, was_correct=outcome)
    assert ece.n_total == 100
    assert ece.last_ece < 0.15, f"ECE should be reasonable (<0.15): {ece.last_ece}"
    print(f"  Test 4a PASS: ece={ece.last_ece:.4f}")

    # Tail summary
    tail = ece.get_tail_summary()
    assert "high_confidence_tail" in tail
    assert "low_confidence_tail" in tail
    print(f"  Test 4b PASS: tail_summary keys present")

    # Serialization
    d4 = ece.to_dict()
    ece2 = RollingECE.from_dict(d4)
    assert abs(ece2.last_ece - ece.last_ece) < 0.0001
    print(f"  Test 4c PASS: serialization round-trip")

    # ── Test 5: DegradationMonitor (full integration) ──
    print("\n--- Test 5: DegradationMonitor (integration) ---")
    import tempfile
    tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    monitor = DegradationMonitor(cache_file=tmp.name)

    # Record 200 trades across multiple stations
    stations = ["KNYC", "KATL", "KSEA", "KDFW", "KDEN"]
    import random
    random.seed(42)
    for i in range(200):
        station = random.choice(stations)
        direction = random.choice(["up", "down"])
        conf = 0.75 + random.uniform(-0.1, 0.1)
        correct = random.random() < 0.55  # ~55% accuracy
        monitor.record_trade(station, direction, conf, correct)

    # Check status
    status = monitor.get_status("global")
    assert "ewma" in status["global"]
    assert "sprt" in status["global"]
    assert "cusum" in status["global"]
    assert "ece" in status["global"]
    print(f"  Test 5a PASS: global status={status['global']['ewma']['accuracy_30d_ewma']:.3f}")

    # Station status
    station_status = monitor.get_status("station")
    assert "stations" in station_status
    assert "KNYC" in station_status["stations"]
    print(f"  Test 5b PASS: {len(station_status['stations'])} stations tracked")

    # Direction status
    dir_status = monitor.get_status("direction")
    assert "directions" in dir_status
    print(f"  Test 5c PASS: direction tracking active")

    # Full report
    report = monitor.generate_report()
    assert "global" in report
    assert "stations" in report
    assert "state" in report
    assert "thresholds" in report
    print(f"  Test 5d PASS: report generated, alert_level={report['state']['last_alert_level']}")

    # Calibration drift check
    drift_result = monitor.check_calibration_drift({"calibrators": {}, "_fallback": {}})
    assert "note" in drift_result
    print(f"  Test 5e PASS: drift check: {drift_result['note']}")

    # Save report
    report_path = monitor.save_report(path="/tmp/test_degradation_report.json")
    import os as _os
    assert _os.path.exists(report_path)
    print(f"  Test 5f PASS: report saved to {report_path}")
    _os.unlink(report_path)

    # Cache persistence
    saved_n = monitor.global_ewma.n
    monitor2 = DegradationMonitor(cache_file=tmp.name)
    assert monitor2.global_ewma.n == saved_n, f"Cache should restore n: {monitor2.global_ewma.n} vs {saved_n}"
    print(f"  Test 5g PASS: cache persistence, n={saved_n}")
    _os.unlink(tmp.name)

    # ── Test 6: Edge cases ──
    print("\n--- Test 6: Edge cases ---")
    empty_mon = DegradationMonitor(cache_file="/tmp/empty_test.json")
    empty_status = empty_mon.get_status()
    assert "global" in empty_status
    assert empty_status["global"]["ewma"]["n_total"] == 0
    print("  Test 6a PASS: empty monitor works")

    # Single trade
    empty_mon.record_trade("KNYC", "up", 0.75, was_correct=True)
    assert empty_mon.global_ewma.n == 1
    print(f"  Test 6b PASS: single trade, accuracy={empty_mon.global_ewma.accuracy_short:.4f}")

    # Mark recalibration
    empty_mon.mark_recalibration()
    assert empty_mon._trades_since_recalibration == 0
    empty_mon.record_trade("KNYC", "up", 0.75, was_correct=True)
    assert empty_mon._trades_since_recalibration == 1
    print("  Test 6c PASS: recalibration tracking")

    # Cleanup
    try:
        _os.unlink("/tmp/empty_test.json")
    except Exception:
        pass

    print("\n" + "=" * 60)
    print("ALL SELF-TESTS PASSED")
    print("=" * 60)
    return True


# ──────────────────────────────────────────────
# CLI entry point
# ──────────────────────────────────────────────

def main():
    """CLI entry point: generate and save a degradation report."""
    import argparse

    parser = argparse.ArgumentParser(description="Degradation Monitor CLI")
    parser.add_argument("--cache-file", default=DEFAULT_CACHE_FILE,
                        help=f"Cache file path (default: {DEFAULT_CACHE_FILE})")
    parser.add_argument("--report-dir", default=REPORT_DIR,
                        help=f"Report directory (default: {REPORT_DIR})")
    parser.add_argument("--report-path", default=None,
                        help="Explicit report output path (default: auto-named)")
    parser.add_argument("--self-test", action="store_true",
                        help="Run self-test suite")
    parser.add_argument("--status", action="store_true",
                        help="Print current monitoring status")

    args = parser.parse_args()

    if args.self_test:
        _self_test()
        return

    monitor = DegradationMonitor(
        cache_file=args.cache_file,
        report_dir=args.report_dir,
    )

    if args.status:
        status = monitor.get_status("all")
        print(json.dumps(status, indent=2))
        return

    # Default: generate and save report
    path = monitor.save_report(path=args.report_path)
    print(f"Report saved to: {path}")

    # Print alert summary
    if monitor._last_alert_level:
        print(f"Alert level: {monitor._last_alert_level}")
        print(f"Global EWMA accuracy: {monitor.global_ewma.accuracy_short:.3f}")
        print(f"Global ECE: {monitor.global_ece.last_ece:.4f}")
    else:
        print("No active alerts. System normal.")


if __name__ == "__main__":
    main()
