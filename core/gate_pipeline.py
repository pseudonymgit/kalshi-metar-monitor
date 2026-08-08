#!/usr/bin/env python3
"""
gate_pipeline.py — Orchestrated 7-Gate Pipeline for Phase 2 Meta-Sweep

Gate order (deterministic, cheapest-first):
  1. Settlement        — market open, weekday, cooldown
  2. Station Skill     — Brier Skill Score filter
  3. Liquidity         — spread vs edge filter
  4. Agreement         — N-of-M consensus
  5. Trajectory        — analog trajectory confirmation
  6. Adaptive Threshold — Bayesian Beta-Bernoulli confidence floor
  7. Production        — real-money readiness

B-Mode compliant. No AI/ML inside the pipeline loop.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np


@dataclass
class GateResult:
    """Result of running the full gate pipeline for one trade candidate."""
    passed: bool
    reason: str                     # Human-readable summary of which gate blocked
    gate_stats: Dict[str, dict]     # Per-gate pass/fail with details
    modulated_confidence: float = 0.0
    modulated_direction: Optional[str] = None


class GatePipeline:
    """Orchestrates the 7-gate pipeline in fixed order.

    Each gate is lazily initialised from the configurations dict.  Gates that
    are disabled return a no-op pass immediately.
    """

    def __init__(self, meta_config: Dict[str, Any]):
        """
        meta_config should contain gate-parameter keys as described in
        SWEEP-REVISION-SPEC §4.1.  Typical keys:

            agreement_n, agreement_m, settlement_cooldown_hours,
            bss_threshold, spread_threshold, min_analogs,
            prior_alpha, prior_beta, production_min_accuracy, etc.
        """
        self.cfg = meta_config

        # Lazy gate singletons – populated on first use
        self._settlement_gate = None
        self._station_skill_gate = None
        self._liquidity_gate = None
        self._agreement_gate = None
        self._trajectory_gate = None
        self._adaptive_registry = None
        self._production_gate = None

    # ------------------------------------------------------------------
    # Lazy init helpers (imported inline to avoid circular deps at module
    # load time)
    # ------------------------------------------------------------------

    def _get_settlement_gate(self):
        if self._settlement_gate is not None:
            return self._settlement_gate
        if not self.cfg.get("settlement_gate_enabled", False):
            self._settlement_gate = "DISABLED"
            return self._settlement_gate
        from core.settlement_execution_gate import SettlementExecutionGate
        self._settlement_gate = SettlementExecutionGate(
            db_path=self.cfg.get("settlements_db", None),
        )
        return self._settlement_gate

    def _get_station_skill_gate(self):
        if self._station_skill_gate is not None:
            return self._station_skill_gate
        if not self.cfg.get("station_skill_gate_enabled", False):
            self._station_skill_gate = "DISABLED"
            return self._station_skill_gate
        from core.station_skill_gate import StationSkillGate
        self._station_skill_gate = StationSkillGate(
            metar_db_path=self.cfg.get("metar_db", ""),
        )
        return self._station_skill_gate

    def _get_liquidity_gate(self):
        if self._liquidity_gate is not None:
            return self._liquidity_gate
        if not self.cfg.get("liquidity_gate_enabled", False):
            self._liquidity_gate = "DISABLED"
            return self._liquidity_gate
        from core.liquidity_gate import LiquidityGate
        self._liquidity_gate = LiquidityGate(
            db_path=self.cfg.get("metar_db", None),
        )
        return self._liquidity_gate

    def _get_agreement_gate(self):
        if self._agreement_gate is not None:
            return self._agreement_gate
        if not self.cfg.get("agreement_gate_enabled", False):
            self._agreement_gate = "DISABLED"
            return self._agreement_gate
        from core.agreement_gate import AgreementGate
        self._agreement_gate = AgreementGate(
            n_required=self.cfg.get("agreement_n", 3),
            m_total=self.cfg.get("agreement_m", 9),
        )
        return self._agreement_gate

    def _get_trajectory_gate(self):
        if self._trajectory_gate is not None:
            return self._trajectory_gate
        if not self.cfg.get("trajectory_gate_enabled", False):
            self._trajectory_gate = "DISABLED"
            return self._trajectory_gate
        from core.trajectory_confirmation_gate import TrajectoryConfirmationGate
        self._trajectory_gate = TrajectoryConfirmationGate(
            min_analogs=self.cfg.get("min_analogs", 30),
        )
        return self._trajectory_gate

    def _get_adaptive_registry(self):
        if self._adaptive_registry is not None:
            return self._adaptive_registry
        if not self.cfg.get("adaptive_thresholds_enabled", False):
            self._adaptive_registry = "DISABLED"
            return self._adaptive_registry
        from core.adaptive_thresholds import AdaptiveThresholdRegistry
        self._adaptive_registry = AdaptiveThresholdRegistry(
            db_path=self.cfg.get("metar_db", ""),
        )
        return self._adaptive_registry

    def _get_production_gate(self):
        if self._production_gate is not None:
            return self._production_gate
        if not self.cfg.get("production_gate_enabled", False):
            self._production_gate = "DISABLED"
            return self._production_gate
        from core.production_gate import ProductionGate
        self._production_gate = ProductionGate()
        return self._production_gate

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def evaluate(
        self,
        signal_name: str,
        station: str,
        date_str: str,
        direction: Union[str, int],
        confidence: float,
        market_type: str = "HIGH",
        settlements_data: Optional[dict] = None,
    ) -> GateResult:
        """Run all 7 gates in order.

        Parameters
        ----------
        signal_name : str
        station : str          ICAO code
        date_str : str         YYYY-MM-DD
        direction : str|int    'up'/'down'/1/-1
        confidence : float     0..1
        market_type : str      HIGH / LOW
        settlements_data : dict   Station -> {date: temp} for trajectory gate

        Returns
        -------
        GateResult with .passed, .reason, .gate_stats, and possibly a
        modulated confidence/direction from gates that adjust the signal.
        """
        gate_stats: Dict[str, dict] = {}
        mod_conf = float(confidence)
        mod_dir = direction

        # Normalise direction
        if isinstance(mod_dir, int):
            mod_dir = "up" if mod_dir > 0 else "down"

        # ----------------------------------------------------------
        # Gate 1 — Settlement Execution Gate
        # ----------------------------------------------------------
        sg = self._get_settlement_gate()
        if sg == "DISABLED":
            gate_stats["settlement"] = {"passed": True, "reason": "disabled"}
        else:
            epoch_id = f"{date_str}_{market_type}"
            try:
                result = sg.evaluate(
                    station=station,
                    trading_date=date_str,
                    epoch_id=epoch_id,
                )
                passed = result.verdict.is_pass() if hasattr(result, "verdict") else True
                gate_stats["settlement"] = {
                    "passed": bool(passed),
                    "reason": str(result.verdict) if hasattr(result, "verdict") else "pass",
                }
                if not passed:
                    return GateResult(False, f"Settlement gate blocked: {result.verdict}", gate_stats, mod_conf, mod_dir)
            except Exception as exc:
                gate_stats["settlement"] = {"passed": False, "reason": f"error: {exc}"}
                return GateResult(False, f"Settlement gate error: {exc}", gate_stats, mod_conf, mod_dir)

        # ----------------------------------------------------------
        # Gate 2 — Station Skill Gate
        # ----------------------------------------------------------
        ssg = self._get_station_skill_gate()
        if ssg == "DISABLED":
            gate_stats["station_skill"] = {"passed": True, "reason": "disabled"}
        else:
            try:
                bss_matrix = ssg.get_bss_matrix()
                bss_val = bss_matrix.get(station, {}).get(market_type, -999.0)
                threshold = self.cfg.get("bss_threshold", 0.0)
                passed = bss_val > threshold
                gate_stats["station_skill"] = {
                    "passed": bool(passed),
                    "bss": float(bss_val),
                    "threshold": float(threshold),
                    "reason": f"BSS={bss_val:.4f} vs threshold={threshold}",
                }
                if not passed:
                    return GateResult(False, f"Station Skill Gate blocked: BSS={bss_val:.4f} <= {threshold}", gate_stats, mod_conf, mod_dir)
            except Exception as exc:
                gate_stats["station_skill"] = {"passed": False, "reason": f"error: {exc}"}
                return GateResult(False, f"Station Skill Gate error: {exc}", gate_stats, mod_conf, mod_dir)

        # ----------------------------------------------------------
        # Gate 3 — Liquidity Gate
        # ----------------------------------------------------------
        lg = self._get_liquidity_gate()
        if lg == "DISABLED":
            gate_stats["liquidity"] = {"passed": True, "reason": "disabled"}
        else:
            try:
                edge = abs(confidence - 0.5) * 2.0  # simple edge proxy
                passed = lg.evaluate(
                    station=station,
                    date=date_str,
                    signal_edge=edge,
                    direction=mod_dir,
                )
                gate_stats["liquidity"] = {
                    "passed": bool(passed),
                    "edge": float(edge),
                    "reason": "pass" if passed else "blocked",
                }
                if not passed:
                    return GateResult(False, f"Liquidity Gate blocked (edge={edge:.4f})", gate_stats, mod_conf, mod_dir)
            except Exception as exc:
                gate_stats["liquidity"] = {"passed": False, "reason": f"error: {exc}"}
                return GateResult(False, f"Liquidity Gate error: {exc}", gate_stats, mod_conf, mod_dir)

        # ----------------------------------------------------------
        # Gate 4 — Agreement Gate (N-of-M)
        # ----------------------------------------------------------
        ag = self._get_agreement_gate()
        if ag == "DISABLED":
            gate_stats["agreement"] = {"passed": True, "reason": "disabled"}
        else:
            try:
                # Build minimal signal tuples for the agreement gate
                signals = [(station, market_type, mod_dir.upper(), signal_name)]
                filtered = ag.filter_signals(signals)
                passed = len(filtered) > 0
                gate_stats["agreement"] = {
                    "passed": bool(passed),
                    "n_signals_in": len(signals),
                    "n_signals_out": len(filtered),
                    "reason": f"N-of-M: {len(filtered)}/{len(signals)} passed",
                }
                if not passed:
                    return GateResult(False, f"Agreement Gate blocked: 0/{len(signals)} passed", gate_stats, mod_conf, mod_dir)
            except Exception as exc:
                gate_stats["agreement"] = {"passed": False, "reason": f"error: {exc}"}
                return GateResult(False, f"Agreement Gate error: {exc}", gate_stats, mod_conf, mod_dir)

        # ----------------------------------------------------------
        # Gate 5 — Trajectory Confirmation Gate
        # ----------------------------------------------------------
        tg = self._get_trajectory_gate()
        if tg == "DISABLED":
            gate_stats["trajectory"] = {"passed": True, "reason": "disabled"}
        else:
            try:
                from core.trajectory_confirmation_gate import evaluate_gate_for_station_date
                verdict, mod_conf_traj = evaluate_gate_for_station_date(
                    station, date_str, mod_dir, mod_conf, tg
                )[:2]
                passed = verdict in ("CONFIRM", "NEUTRAL")
                gate_stats["trajectory"] = {
                    "passed": bool(passed),
                    "verdict": str(verdict),
                    "modulated_confidence": float(mod_conf_traj),
                    "reason": f"verdict={verdict}",
                }
                if passed:
                    mod_conf = float(mod_conf_traj)
                else:
                    return GateResult(False, f"Trajectory Gate blocked: verdict={verdict}", gate_stats, mod_conf, mod_dir)
            except Exception as exc:
                gate_stats["trajectory"] = {"passed": False, "reason": f"error: {exc}"}
                return GateResult(False, f"Trajectory Gate error: {exc}", gate_stats, mod_conf, mod_dir)

        # ----------------------------------------------------------
        # Gate 6 — Adaptive Threshold Registry
        # ----------------------------------------------------------
        ar = self._get_adaptive_registry()
        if ar == "DISABLED":
            gate_stats["adaptive"] = {"passed": True, "reason": "disabled"}
        else:
            try:
                threshold = ar.get_threshold(signal_name, station)
                passed = mod_conf >= threshold
                gate_stats["adaptive"] = {
                    "passed": bool(passed),
                    "confidence": float(mod_conf),
                    "threshold": float(threshold),
                    "reason": f"conf={mod_conf:.4f} >= threshold={threshold:.4f}" if passed else f"conf={mod_conf:.4f} < threshold={threshold:.4f}",
                }
                if not passed:
                    return GateResult(False, f"Adaptive Threshold blocked: {mod_conf:.4f} < {threshold:.4f}", gate_stats, mod_conf, mod_dir)
            except Exception as exc:
                gate_stats["adaptive"] = {"passed": False, "reason": f"error: {exc}"}
                return GateResult(False, f"Adaptive Threshold error: {exc}", gate_stats, mod_conf, mod_dir)

        # ----------------------------------------------------------
        # Gate 7 — Production Gate
        # ----------------------------------------------------------
        pg = self._get_production_gate()
        if pg == "DISABLED":
            gate_stats["production"] = {"passed": True, "reason": "disabled"}
        else:
            try:
                passes, failures = pg.meets_requirements(
                    accuracy=mod_conf,  # proxy: treat confidence as expected accuracy
                    total_trades=self.cfg.get("production_min_trades", 100),
                    station_trade_counts={station: 1},
                    sharpe=self.cfg.get("production_min_sharpe", 1.0),
                )
                gate_stats["production"] = {
                    "passed": bool(passes),
                    "failures": list(failures) if failures else [],
                    "reason": "pass" if passes else f"failed: {failures}",
                }
                if not passes:
                    return GateResult(False, f"Production Gate blocked: {failures}", gate_stats, mod_conf, mod_dir)
            except Exception as exc:
                gate_stats["production"] = {"passed": False, "reason": f"error: {exc}"}
                return GateResult(False, f"Production Gate error: {exc}", gate_stats, mod_conf, mod_dir)

        return GateResult(True, "All gates passed", gate_stats, mod_conf, mod_dir)

    def reset_adaptive(self) -> None:
        """Reset adaptive registry if present (useful between sweep phases)."""
        if self._adaptive_registry is not None and self._adaptive_registry != "DISABLED":
            from core.adaptive_thresholds import AdaptiveThresholdRegistry
            self._adaptive_registry = AdaptiveThresholdRegistry()


# Convenience function for Phase 2 sweep orchestration
def run_gate_pipeline(
    meta_config: Dict[str, Any],
    signal_name: str,
    station: str,
    date_str: str,
    direction: Union[str, int],
    confidence: float,
    market_type: str = "HIGH",
    settlements_data: Optional[dict] = None,
) -> GateResult:
    """One-shot convenience wrapper."""
    pipeline = GatePipeline(meta_config)
    return pipeline.evaluate(signal_name, station, date_str, direction, confidence, market_type, settlements_data)