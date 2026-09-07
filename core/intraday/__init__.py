"""
Intraday Module — Time-of-Day Signal Stack

Entry-point for intraday signal evaluation, fusion, calibration,
recalibration, and entry timing.

Stack:
  1. HourlyPipeline — aggregates METAR/ASOS/NWP into hourly feature vectors
  2. HourlySignalEvaluator — runs intraday signals on each hour bucket
  3. HourlyFusionEngine — LOOP fusion of hourly signals
  4. HourlyCalibration — calibrated probability per (station, hour_before_settlement)
  5. EntryTimingEngine — risk-gated entry decisions
  6. ContinuousRecalibrationLoop — Kalman-ish belief update
"""

from .pipeline import HourlyPipeline
from .signal_evaluator import HourlySignalEvaluator
from .fusion import HourlyFusionEngine, FusedResult
from .calibration import HourlyCalibration
from .entry_engine import EntryTimingEngine, EntrySignal, RiskGuardrails
from .recalibration_loop import ContinuousRecalibrationLoop

__all__ = [
    "HourlyPipeline",
    "HourlySignalEvaluator",
    "HourlyFusionEngine",
    "FusedResult",
    "HourlyCalibration",
    "EntryTimingEngine",
    "EntrySignal",
    "RiskGuardrails",
    "ContinuousRecalibrationLoop",
]