"""
ContinuousRecalibrationLoop — Hourly Recalibration of Probabilities (v1.0 — 2026-09-07)

Maintains a Kalman-ish belief update over calibrated probabilities across
successive hours for the same settlement. As new hourly observations arrive,
updates the probability estimate using a simple recursive Bayesian update.

This is NOT a full Kalman filter — it's a lightweight belief update:

  updated_prob = (prior_prob * prior_weight + new_prob * new_weight) / total_weight
  updated_weight = prior_weight + new_weight

Where new_weight is a function of:
  - The signal's confidence (higher confidence → more weight)
  - Hour proximity to settlement (closer to settlement → more weight)
  - Signal consistency (if new signal agrees with prior → bonus weight)

Also tracks performance statistics (accuracy, Brier score) per (station, hb)
for use in the HourlyCalibration's drift detection.

Deterministic math only — no AI/ML.
"""

import json
import logging
import os
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# ─── Defaults ──────────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_STATE_PATH = str(REPO_ROOT / "data" / "intraday" / "recalibration_state.json")

# Weight parameters
SIGNAL_WEIGHT_BASE = 1.0          # Base weight for each new signal
CONSISTENCY_BONUS = 0.5            # Extra weight when new signal matches prior
PROXIMITY_WEIGHT_FACTOR = 0.15     # Extra weight per hour closer to settlement
MAX_WINDOW_HOURS = 24              # Keep last N hours of data per (station, hb)
STALE_HOURS = 48                   # Prune states not updated in this many hours

# Performance tracking
PERFORMANCE_WINDOW = 100           # Max entries per (station, hb) calibration cell


class ContinuousRecalibrationLoop:
    """
    Continuous recalibration of intraday probabilities across hours.

    Maintains a running belief state per (station, hour_before_settlement)
    and updates it when new calibrated probabilities arrive.

    Usage:
        loop = ContinuousRecalibrationLoop()
        new_prob, new_weight = loop.update_belief(
            station="KATL", hour_before_settlement=4,
            prior_belief=0.62, prior_weight=3.0,
            new_probability=0.65, confidence=0.7,
        )
        loop.record_settlement(station="KATL", date="...", predicted=0.62, outcome=1.0)
        perf = loop.get_performance("KATL", 4)
    """

    def __init__(self, state_path: str = ""):
        self.state_path = state_path or DEFAULT_STATE_PATH
        # belief_state[station][hb_str] = {"probability": float, "weight": float, "updated": int(timestamp)}
        self.belief_state: Dict[str, Dict[str, Dict]] = defaultdict(lambda: defaultdict(dict))
        # performance[station][hb_str] = list of {"predicted": float, "outcome": float, "ts": int}
        self.performance: Dict[str, Dict[str, List[Dict]]] = defaultdict(lambda: defaultdict(list))
        self._load_state()

    # ─── Persistence ──────────────────────────────────────────

    def _load_state(self) -> None:
        """Load saved recalibration state from disk."""
        if not self.state_path or not os.path.exists(self.state_path):
            logger.info("No prior recalibration state found — starting fresh")
            return

        try:
            with open(self.state_path) as f:
                data = json.load(f)

            for station, hb_data in data.get("belief_state", {}).items():
                for hb_key, entry in hb_data.items():
                    self.belief_state[station][hb_key] = entry

            for station, hb_data in data.get("performance", {}).items():
                for hb_key, entries in hb_data.items():
                    self.performance[station][hb_key] = entries

            logger.info(
                f"Loaded recalibration state: "
                f"{sum(len(s) for s in self.belief_state.values())} cells, "
                f"{sum(len(p) for p in self.performance.values())} perf entries"
            )
        except Exception as e:
            logger.warning(f"Failed to load recalibration state: {e}")

    def _save_state(self) -> None:
        """Save current recalibration state to disk."""
        if not self.state_path:
            return

        try:
            os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
            data = {
                "belief_state": dict(self.belief_state),
                "performance": dict(self.performance),
                "saved_ts": int(time.time()),
            }
            with open(self.state_path, "w") as f:
                json.dump(data, f, indent=2)
            logger.debug(f"Saved recalibration state to {self.state_path}")
        except Exception as e:
            logger.warning(f"Failed to save recalibration state: {e}")

    # ─── Belief Update ────────────────────────────────────────

    def _compute_weight(
        self,
        confidence: float,
        hour_before_settlement: int,
        is_consistent: bool = False,
    ) -> float:
        """
        Compute the weight for a new probability observation.

        Weight = base_weight + proximity_bonus + consistency_bonus

        Args:
            confidence: Signal confidence [0, 1]
            hour_before_settlement: Hours until settlement
            is_consistent: Whether this signal agrees with prior belief

        Returns:
            Weight (>= 0)
        """
        weight = SIGNAL_WEIGHT_BASE

        # Confidence multiplier: signals with confidence > 0.5 get more weight
        if confidence > 0.5:
            weight += (confidence - 0.5) * 2.0  # Max +1.0 at conf=1.0

        # Proximity: closer to settlement → higher weight
        if hour_before_settlement > 0:
            proximity_weight = PROXIMITY_WEIGHT_FACTOR * max(0, 6 - hour_before_settlement)
            weight += proximity_weight

        # Consistency bonus
        if is_consistent:
            weight += CONSISTENCY_BONUS

        return max(0.1, weight)

    def update_belief(
        self,
        station: str,
        hour_before_settlement: int,
        prior_belief: float,
        prior_weight: float,
        new_probability: float,
        confidence: float,
    ) -> Tuple[float, float]:
        """
        Update belief for a (station, hb) cell with a new observation.

        Recursive Bayesian update using weighted averaging:
          updated_prob = (prior_belief * prior_weight + new_prob * new_weight) / (prior_weight + new_weight)
          updated_weight = prior_weight + new_weight

        Args:
            station: ICAO station code
            hour_before_settlement: Hours until settlement
            prior_belief: Previous probability estimate
            prior_weight: Previous weight (quality of the estimate)
            new_probability: New calibrated probability from this hour
            confidence: Confidence in the new signal [0, 1]

        Returns:
            (updated_probability, updated_weight)
        """
        hb_key = str(hour_before_settlement)

        # Check consistency with prior belief
        direction_prior = prior_belief >= 0.5
        direction_new = new_probability >= 0.5
        is_consistent = direction_prior == direction_new

        new_weight = self._compute_weight(confidence, hour_before_settlement, is_consistent)

        total_weight = prior_weight + new_weight
        if total_weight > 0:
            updated_prob = (
                (prior_belief * prior_weight) + (new_probability * new_weight)
            ) / total_weight
        else:
            updated_prob = new_probability

        updated_weight = min(prior_weight + new_weight, 100.0)  # Cap weight to prevent runaway

        # Store in belief state
        now_ts = int(time.time())
        self.belief_state[station][hb_key] = {
            "probability": round(updated_prob, 6),
            "weight": round(updated_weight, 4),
            "updated": now_ts,
        }

        logger.debug(
            f"Belief update: {station} hb={hb_key}: "
            f"{prior_belief:.4f}(w={prior_weight:.1f}) + "
            f"{new_probability:.4f}(w={new_weight:.1f}) → "
            f"{updated_prob:.4f}(w={updated_weight:.1f})"
        )

        # Periodically save state
        if int(time.time()) % 300 < 10:  # Every ~5 minutes
            self._save_state()

        return float(np.clip(updated_prob, 0.0, 1.0)), updated_weight

    def get_belief(
        self, station: str, hour_before_settlement: int
    ) -> Optional[Tuple[float, float]]:
        """
        Get the current belief state for a (station, hb) cell.

        Returns (probability, weight) or None if no state exists.
        """
        hb_key = str(hour_before_settlement)
        entry = self.belief_state.get(station, {}).get(hb_key)
        if entry:
            return entry.get("probability", 0.5), entry.get("weight", 0.0)
        return None

    def get_combined_belief(
        self,
        station: str,
        hour_before_settlement: int,
        current_probability: float,
        confidence: float,
    ) -> float:
        """
        Get the combined belief: merge current probability with prior belief if it exists.

        If no prior belief exists, initialize one with current probability.

        Args:
            station: ICAO station code
            hour_before_settlement: Hours until settlement
            current_probability: Current calibrated probability
            confidence: Confidence in the current signal

        Returns:
            Combined probability
        """
        prior = self.get_belief(station, hour_before_settlement)
        if prior is None:
            # Initialize belief with current value
            now_ts = int(time.time())
            hb_key = str(hour_before_settlement)
            self.belief_state[station][hb_key] = {
                "probability": round(current_probability, 6),
                "weight": SIGNAL_WEIGHT_BASE,
                "updated": now_ts,
            }
            return float(np.clip(current_probability, 0.0, 1.0))

        prior_prob, prior_weight = prior
        if prior_weight < 0.1:
            return float(np.clip(current_probability, 0.0, 1.0))

        # Update belief
        updated_prob, _ = self.update_belief(
            station=station,
            hour_before_settlement=hour_before_settlement,
            prior_belief=prior_prob,
            prior_weight=prior_weight,
            new_probability=current_probability,
            confidence=confidence,
        )
        return updated_prob

    # ─── Settlement Recording ─────────────────────────────────

    def record_settlement(
        self,
        station: str,
        date_str: str,
        hour_before_settlement: int,
        predicted_probability: float,
        outcome: float,
    ) -> None:
        """
        Record a settlement outcome for calibration tracking.

        Args:
            station: ICAO station code
            date_str: Settlement date "YYYY-MM-DD"
            hour_before_settlement: Hours before settlement
            predicted_probability: Our predicted probability [0, 1]
            outcome: Settlement outcome (0.0 or 1.0)
        """
        hb_key = str(hour_before_settlement)

        self.performance[station][hb_key].append({
            "predicted": round(predicted_probability, 6),
            "outcome": float(outcome),
            "ts": int(time.time()),
            "date": date_str,
        })

        # Trim to performance window
        entries = self.performance[station][hb_key]
        if len(entries) > PERFORMANCE_WINDOW:
            self.performance[station][hb_key] = entries[-PERFORMANCE_WINDOW:]

        logger.debug(
            f"Settlement recorded: {station} hb={hb_key}: "
            f"pred={predicted_probability:.4f} outcome={outcome:.1f}"
        )

    # ─── Performance Metrics ──────────────────────────────────

    def get_performance(
        self, station: str, hour_before_settlement: int
    ) -> Dict:
        """
        Get performance metrics for a (station, hb) calibration cell.

        Returns:
            Dict with:
              - n: number of recorded outcomes
              - accuracy: directional accuracy
              - brier: Brier score
              - mean_predicted: average predicted probability
              - mean_outcome: average actual outcome
              - log_loss: cross-entropy loss
        """
        hb_key = str(hour_before_settlement)
        entries = self.performance.get(station, {}).get(hb_key, [])

        if not entries:
            return {"n": 0, "accuracy": 0.0, "brier": 0.0, "error": "no_data"}

        predicted = np.array([e["predicted"] for e in entries])
        outcomes = np.array([e["outcome"] for e in entries])

        # Directional accuracy
        pred_dirs = (predicted >= 0.5).astype(float)
        actual_dirs = outcomes.astype(float)
        accuracy = float(np.mean(pred_dirs == actual_dirs))

        # Brier score
        brier = float(np.mean((predicted - outcomes) ** 2))

        # Log loss
        clipped = np.clip(predicted, 1e-9, 1 - 1e-9)
        log_loss = float(-np.mean(
            outcomes * np.log(clipped) + (1 - outcomes) * np.log(1 - clipped)
        ))

        return {
            "n": len(entries),
            "accuracy": round(accuracy, 4),
            "brier": round(brier, 4),
            "log_loss": round(log_loss, 4),
            "mean_predicted": round(float(np.mean(predicted)), 4),
            "mean_outcome": round(float(np.mean(outcomes)), 4),
        }

    def get_all_performance(self) -> Dict[str, Dict]:
        """
        Get performance metrics for all (station, hb) cells.

        Returns:
            Dict[station][hb_key] -> performance dict
        """
        results: Dict[str, Dict] = {}
        for station in self.performance:
            results[station] = {}
            for hb_key in self.performance[station]:
                entry = self.get_performance(station, int(hb_key))
                if entry.get("n", 0) >= 10:
                    results[station][hb_key] = entry
        return results

    # ─── State Management ─────────────────────────────────────

    def prune_stale(self, max_stale_hours: int = STALE_HOURS) -> int:
        """
        Remove cells not updated in max_stale_hours.

        Returns number of cells pruned.
        """
        now = int(time.time())
        cutoff = now - (max_stale_hours * 3600)
        pruned = 0

        stations_to_prune = []
        for station, hb_data in list(self.belief_state.items()):
            hbs_to_prune = []
            for hb_key, entry in list(hb_data.items()):
                if entry.get("updated", 0) < cutoff:
                    hbs_to_prune.append(hb_key)
                    pruned += 1
            for hb_key in hbs_to_prune:
                del self.belief_state[station][hb_key]
            if not self.belief_state[station]:
                stations_to_prune.append(station)

        for station in stations_to_prune:
            del self.belief_state[station]

        if pruned:
            logger.info(f"Pruned {pruned} stale belief cells")
            self._save_state()

        return pruned

    def to_json_dict(self) -> Dict:
        """Serialize the recalibration state to a JSON-compatible dict."""
        return {
            "belief_state": dict(self.belief_state),
            "performance": dict(self.performance),
            "generated_ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

    def save(self) -> None:
        """Save the recalibration state to disk."""
        self._save_state()

    @classmethod
    def load(cls, state_path: str = "") -> "ContinuousRecalibrationLoop":
        """Create a ContinuousRecalibrationLoop from saved state."""
        return cls(state_path=state_path)