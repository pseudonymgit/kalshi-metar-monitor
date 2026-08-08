#!/usr/bin/env python3
"""
modulator_stack.py — Signal Modulation Pipeline (Fusion → Spatial → Calibration)

Modulation order:
  1. Signal Fusion       — combine multiple signal predictions (UWC / majority / weighted)
  2. Spatial Coherence   — cross-station confidence modulation
  3. Calibration         — Platt / BMA / EMOS / both

Each stage is optional (disabled if the corresponding config flag is off).

B-Mode compliant. No AI/ML inside the modulator pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np


@dataclass
class ModulatedResult:
    """Result of applying the full modulator stack."""
    direction: Optional[str]          # 'up' / 'down' / None
    confidence: float                 # 0..1
    stage_results: Dict[str, dict]    # Per-stage diagnostics
    n_stages_applied: int             # How many stages actually ran


class ModulatorStack:
    """Applies fusion → spatial → calibration in fixed order.

    Parameters (from meta_config)
    -----------------------------
    fusion_mode : int          0=none, 1=uwc, 2=majority, 3=weighted
    spatial_coherence_enabled : bool
    calibration_mode : int     0=platt, 1=bma, 2=emos, 3=both
    """

    def __init__(self, meta_config: Dict[str, Any]):
        self.cfg = meta_config

        # Lazy-loaded singletons
        self._cascade = None
        self._spatial_gate = None
        self._platt_pipeline = None

    # ------------------------------------------------------------------
    # Lazy init helpers
    # ------------------------------------------------------------------

    def _get_cascade(self):
        if self._cascade is not None:
            return self._cascade
        mode_code = int(self.cfg.get("fusion_mode", 0))
        if mode_code == 0:
            self._cascade = "DISABLED"
            return self._cascade
        mode_map = {1: "uwc", 2: "majority", 3: "weighted"}
        mode_str = mode_map.get(mode_code, "uwc")
        try:
            from core.signal_fusion import UncertaintyWeightedCascade, FusionModeConfig
            fusion_cfg = FusionModeConfig(mode=mode_str)
            self._cascade = UncertaintyWeightedCascade(fusion_cfg)
        except Exception:
            self._cascade = "DISABLED"
        return self._cascade

    def _get_spatial_gate(self):
        if self._spatial_gate is not None:
            return self._spatial_gate
        if not self.cfg.get("spatial_coherence_enabled", False):
            self._spatial_gate = "DISABLED"
            return self._spatial_gate
        try:
            from core.spatial_coherence import SpatialCoherenceGate
            self._spatial_gate = SpatialCoherenceGate()
        except Exception:
            self._spatial_gate = "DISABLED"
        return self._spatial_gate

    def _get_platt_pipeline(self):
        if self._platt_pipeline is not None:
            return self._platt_pipeline
        try:
            from core.platt_calibration import PlattCalibrationPipeline
            self._platt_pipeline = PlattCalibrationPipeline()
        except Exception:
            self._platt_pipeline = "DISABLED"
        return self._platt_pipeline

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def apply(
        self,
        station: str,
        signal_name: str,
        direction: str,
        confidence: float,
        date_str: str = "",
        signal_predictions: Optional[Dict[str, Tuple[Union[str, int], float]]] = None,
        nearby_signals: Optional[Dict[str, dict]] = None,
        market_type: str = "HIGH",
    ) -> ModulatedResult:
        """Run all 3 modulation stages in order.

        Parameters
        ----------
        station : str
        signal_name : str
        direction : str            'up' / 'down'
        confidence : float         0..1
        date_str : str             YYYY-MM-DD (for calibration)
        signal_predictions : dict  signal_name -> (direction, confidence) for fusion
        nearby_signals : dict      station -> info dict for spatial coherence
        market_type : str          HIGH / LOW

        Returns
        -------
        ModulatedResult with final direction/confidence and per-stage diagnostics.
        """
        mod_dir = direction
        mod_conf = float(confidence)
        stage_results: Dict[str, dict] = {}
        n_stages = 0

        # ----------------------------------------------------------
        # Stage 1 — Signal Fusion
        # ----------------------------------------------------------
        cascade = self._get_cascade()
        if cascade == "DISABLED":
            stage_results["fusion"] = {"applied": False, "reason": "disabled"}
        elif signal_predictions is None or len(signal_predictions) < 2:
            stage_results["fusion"] = {"applied": False, "reason": "insufficient signals"}
        else:
            try:
                result = cascade.fuse(
                    signal_predictions=signal_predictions,
                    market_price=float(self.cfg.get("market_price", 0.5)),
                    fee_rate=float(self.cfg.get("fee_rate", 0.07)),
                    bankroll=float(self.cfg.get("capital_base", 10000.0)),
                    hours_to_settlement=24.0,
                )
                if result.get("verdict") == "TRADE" and result.get("direction") is not None:
                    mod_dir = result["direction"]
                    mod_conf = float(result.get("confidence", mod_conf))
                    n_stages += 1
                    stage_results["fusion"] = {
                        "applied": True,
                        "direction": mod_dir,
                        "confidence": mod_conf,
                    }
                else:
                    stage_results["fusion"] = {"applied": False, "reason": f"verdict={result.get('verdict')}"}
            except Exception as exc:
                stage_results["fusion"] = {"applied": False, "reason": f"error: {exc}"}

        # ----------------------------------------------------------
        # Stage 2 — Spatial Coherence
        # ----------------------------------------------------------
        sg = self._get_spatial_gate()
        if sg == "DISABLED":
            stage_results["spatial"] = {"applied": False, "reason": "disabled"}
        elif nearby_signals is None or len(nearby_signals) < 1:
            stage_results["spatial"] = {"applied": False, "reason": "no nearby signals"}
        else:
            try:
                pre_conf = mod_conf
                deferred_direction = mod_dir  # preserve for spatial
                new_conf = sg.modulate_confidence(
                    station=station,
                    signal_name=signal_name,
                    direction=mod_dir,
                    confidence=mod_conf,
                    nearby_signals=nearby_signals,
                    date_str=date_str,
                )
                mod_conf = max(0.501, min(0.999, float(new_conf)))
                n_stages += 1
                stage_results["spatial"] = {
                    "applied": True,
                    "before": float(pre_conf),
                    "after": float(mod_conf),
                    "delta": float(mod_conf - pre_conf),
                }
            except Exception as exc:
                stage_results["spatial"] = {"applied": False, "reason": f"error: {exc}"}

        # ----------------------------------------------------------
        # Stage 3 — Calibration
        # ----------------------------------------------------------
        cal_mode = int(self.cfg.get("calibration_mode", 0))
        cal_mode_map = {0: "platt", 1: "bma", 2: "emos", 3: "both"}
        cal_mode_str = cal_mode_map.get(cal_mode, "platt")

        if cal_mode_str == "platt":
            stage_results["calibration"] = self._apply_platt(station, signal_name, mod_dir, mod_conf, date_str, market_type)
            if "confidence" in stage_results["calibration"]:
                mod_conf = float(stage_results["calibration"]["confidence"])
                n_stages += 1
        elif cal_mode_str in ("bma", "emos", "both"):
            stage_results["calibration"] = self._apply_bma_emos(station, signal_name, mod_dir, mod_conf, date_str, cal_mode_str)
            if "confidence" in stage_results["calibration"]:
                mod_conf = float(stage_results["calibration"]["confidence"])
                n_stages += 1
        else:
            stage_results["calibration"] = {"applied": False, "reason": f"unknown mode: {cal_mode_str}"}

        return ModulatedResult(
            direction=mod_dir,
            confidence=max(0.501, min(0.999, mod_conf)),
            stage_results=stage_results,
            n_stages_applied=n_stages,
        )

    # ------------------------------------------------------------------
    # Calibration helpers
    # ------------------------------------------------------------------

    def _apply_platt(
        self,
        station: str,
        signal_name: str,
        direction: str,
        confidence: float,
        date_str: str,
        market_type: str,
    ) -> dict:
        """Apply Platt scaling calibration."""
        pipeline = self._get_platt_pipeline()
        if pipeline == "DISABLED":
            return {"applied": False, "reason": "pipeline unavailable"}

        try:
            platt_conf = pipeline.calibrate(
                station=station,
                direction=direction,
                market_type=market_type,
                signal_name=signal_name,
                raw_conf=confidence,
            )
            if platt_conf < 0.5:
                return {"applied": True, "blocked": True, "reason": f"calibrated_conf={platt_conf:.4f} < 0.5", "confidence": None}
            return {"applied": True, "before": confidence, "confidence": float(platt_conf), "delta": float(platt_conf - confidence)}
        except Exception as exc:
            return {"applied": False, "reason": f"error: {exc}"}

    def _apply_bma_emos(
        self,
        station: str,
        signal_name: str,
        direction: str,
        confidence: float,
        date_str: str,
        mode: str,
    ) -> dict:
        """Apply BMA or EMOS calibration."""
        try:
            from core.bma_emos import bma_calibrate, emos_calibrate

            if mode in ("bma", "both"):
                bma_conf = bma_calibrate(station, (confidence, None))
            else:
                bma_conf = None

            if mode == "emos":
                emos_conf = emos_calibrate(station, (confidence, None))
            else:
                emos_conf = None

            if mode == "both" and bma_conf is not None:
                combined = (float(bma_conf) + float(emos_conf if emos_conf is not None else confidence)) / 2.0
            elif mode == "bma" and bma_conf is not None:
                combined = float(bma_conf)
            elif mode == "emos" and emos_conf is not None:
                combined = float(emos_conf)
            else:
                return {"applied": True, "blocked": False, "confidence": confidence, "note": "no calibration applied"}

            if combined < 0.5:
                return {"applied": True, "blocked": True, "reason": f"calibrated_conf={combined:.4f} < 0.5", "confidence": None}
            return {"applied": True, "before": confidence, "confidence": combined, "delta": combined - confidence}
        except Exception as exc:
            return {"applied": False, "reason": f"error: {exc}"}


# Convenience wrapper
def apply_modulator_stack(
    meta_config: Dict[str, Any],
    station: str,
    signal_name: str,
    direction: str,
    confidence: float,
    date_str: str = "",
    signal_predictions: Optional[Dict[str, Tuple[Union[str, int], float]]] = None,
    nearby_signals: Optional[Dict[str, dict]] = None,
    market_type: str = "HIGH",
) -> ModulatedResult:
    """One-shot convenience wrapper."""
    stack = ModulatorStack(meta_config)
    return stack.apply(station, signal_name, direction, confidence, date_str, signal_predictions, nearby_signals, market_type)