#!/usr/bin/env python3
"""
lane_manager_v2.py — Multi-Lane Evaluation & Blend for Phase 2 Meta-Sweep

Lanes evaluated:
  1. Main Lane  — signal-native direction/confidence (baseline)
  2. Goldilocks  — intraday microstructure transient spike detection
  3. Trajectory  — heavy-informant analog-based probability estimate

Blending strategy:
  - Each lane produces a (direction, confidence, weight) triple.
  - Final output = weighted blend of all active lanes.
  - Goldilocks only fires when it detects a transient spike.
  - Trajectory lane is the heavy informant (configurable weight).

B-Mode compliant. No AI/ML inside the lane loop.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np


@dataclass
class LaneOutput:
    """Output of one lane evaluation."""
    direction: Optional[str]    # 'up' / 'down' / None
    confidence: float           # 0..1
    weight: float               # blend weight [0, 1]
    quality: float              # internal quality metric
    label: str                  # lane name for diagnostics


@dataclass
class LaneBlendResult:
    """Final blended output from all active lanes."""
    direction: Optional[str]
    confidence: float
    lane_outputs: List[LaneOutput]
    n_active_lanes: int


class LaneManagerV2:
    """Evaluates main / Goldilocks / trajectory lanes and blends their outputs.

    Parameters (from meta_config)
    -----------------------------
    goldilocks_lane_enabled : bool
    trajectory_lane_enabled : bool
    trajectory_lane_weight : float     [0.05, 0.30]
    """

    def __init__(self, meta_config: Dict[str, Any]):
        self.cfg = meta_config

        # Lazy-loaded lane singletons
        self._goldilocks_lane = None
        self._trajectory_lane = None

    # ------------------------------------------------------------------
    # Lazy init helpers
    # ------------------------------------------------------------------

    def _get_goldilocks_lane(self):
        if self._goldilocks_lane is not None:
            return self._goldilocks_lane
        if not self.cfg.get("goldilocks_lane_enabled", False):
            self._goldilocks_lane = "DISABLED"
            return self._goldilocks_lane
        try:
            from core.lane_goldilocks import GoldilocksLane
            self._goldilocks_lane = GoldilocksLane()
        except Exception:
            self._goldilocks_lane = "DISABLED"
        return self._goldilocks_lane

    def _get_trajectory_lane(self):
        if self._trajectory_lane is not None:
            return self._trajectory_lane
        if not self.cfg.get("trajectory_lane_enabled", False):
            self._trajectory_lane = "DISABLED"
            return self._trajectory_lane
        try:
            from core.trajectory_lane import TrajectoryLane
            self._trajectory_lane = TrajectoryLane()
        except Exception:
            self._trajectory_lane = "DISABLED"
        return self._trajectory_lane

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def evaluate_all(
        self,
        station: str,
        date_str: str,
        main_direction: str,
        main_confidence: float,
        settlements_data: Optional[dict] = None,
    ) -> LaneBlendResult:
        """Evaluate all active lanes and return blended result.

        Parameters
        ----------
        station : str
        date_str : str            YYYY-MM-DD
        main_direction : str      'up' / 'down'
        main_confidence : float   0..1 (calibrated confidence)
        settlements_data : dict   Station -> {date: temp} (optional, for trajectory lane)

        Returns
        -------
        LaneBlendResult with mixed output.
        """
        lanes: List[LaneOutput] = []

        # --- Lane 1: Main Lane (always active, baseline) ---
        lanes.append(LaneOutput(
            direction=main_direction,
            confidence=main_confidence,
            weight=1.0,
            quality=main_confidence,
            label="main",
        ))

        # --- Lane 2: Goldilocks Lane (transient spike detection) ---
        gold = self._get_goldilocks_lane()
        gold_output = None
        if gold != "DISABLED":
            try:
                results = gold.evaluate_day(station, date_str)
                for r in results:
                    if hasattr(r, "should_trade") and r.should_trade:
                        g_dir = getattr(r, "direction", main_direction)
                        g_conf = getattr(r, "confidence", 0.5)
                        gold_output = LaneOutput(
                            direction=g_dir,
                            confidence=g_conf,
                            weight=0.15,  # Goldilocks is a light informant
                            quality=g_conf,
                            label="goldilocks",
                        )
                        lanes.append(gold_output)
                        break
            except Exception:
                pass

        # --- Lane 3: Trajectory Lane (heavy informant) ---
        traj = self._get_trajectory_lane()
        traj_output = None
        if traj != "DISABLED":
            try:
                lane_weight = float(self.cfg.get("trajectory_lane_weight", 0.20))
                from core.trajectory_lane import (
                    evaluate_lane_for_station_date,
                    apply_trajectory_lane_to_probability,
                )
                lane_result = evaluate_lane_for_station_date(
                    station, date_str, main_confidence, main_direction, traj
                )
                if lane_result:
                    lane_dir, lane_prob, weight, quality, divergence = lane_result[:5]
                    if lane_dir is not None:
                        traj_output = LaneOutput(
                            direction=lane_dir,
                            confidence=float(lane_prob),
                            weight=float(lane_weight),
                            quality=float(quality),
                            label="trajectory",
                        )
                        lanes.append(traj_output)
            except Exception:
                pass

        # --- Blend ---
        return self._blend_lanes(lanes, main_direction, main_confidence)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _blend_lanes(
        self,
        lanes: List[LaneOutput],
        fallback_direction: str,
        fallback_confidence: float,
    ) -> LaneBlendResult:
        """Weighted blend of all lane outputs.

        Strategy:
          - Lanes that agree on direction get amplified.
          - Lanes that disagree pull the confidence toward 0.5.
          - Weights are normalised to sum to 1.0.
        """
        if not lanes:
            return LaneBlendResult(
                direction=fallback_direction,
                confidence=fallback_confidence,
                lane_outputs=[],
                n_active_lanes=0,
            )

        # Normalise weights
        total_weight = sum(l.w for l in lanes)
        if total_weight <= 0:
            return LaneBlendResult(
                direction=fallback_direction,
                confidence=fallback_confidence,
                lane_outputs=lanes,
                n_active_lanes=len(lanes),
            )

        # Weighted confidence, weighted by lane weight
        # If all directions agree, confidence tilts toward the high end.
        # If they disagree, confidence pulls toward 0.5.
        directions_set = {l.direction for l in lanes if l.direction}
        all_agree = len(directions_set) <= 1

        blended_conf = sum(l.confidence * l.weight for l in lanes) / total_weight

        if all_agree:
            blended_conf = min(0.999, blended_conf * 1.05)  # small uplift for consensus
        else:
            # Pull toward 0.5 when lanes disagree
            blended_conf = 0.5 + (blended_conf - 0.5) * 0.8

        # Direction = majority vote by weight
        dir_weight: Dict[str, float] = {}
        for l in lanes:
            if l.direction:
                dir_weight[l.direction] = dir_weight.get(l.direction, 0.0) + l.weight

        if not dir_weight:
            blended_dir = fallback_direction
        else:
            blended_dir = max(dir_weight, key=dir_weight.get)

        return LaneBlendResult(
            direction=blended_dir,
            confidence=max(0.501, min(0.999, blended_conf)),
            lane_outputs=lanes,
            n_active_lanes=len(lanes),
        )


# Convenience wrapper
def evaluate_lanes(
    meta_config: Dict[str, Any],
    station: str,
    date_str: str,
    main_direction: str,
    main_confidence: float,
    settlements_data: Optional[dict] = None,
) -> LaneBlendResult:
    """One-shot convenience wrapper."""
    mgr = LaneManagerV2(meta_config)
    return mgr.evaluate_all(station, date_str, main_direction, main_confidence, settlements_data)