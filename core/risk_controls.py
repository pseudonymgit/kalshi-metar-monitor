#!/usr/bin/env python3
"""Risk Guardrails Phase 1 - Marty Byrde 2026-07-08"""
from dataclasses import dataclass
from typing import Dict, List, Optional
import logging
logger = logging.getLogger(__name__)
@dataclass
class RiskConfig:
    max_daily_loss: float = 300.0
    max_drawdown_pct: float = 10.0
    consecutive_loss_limit: int = 5
    correlation_threshold: float = 0.70
    signal_conflict_threshold: float = 0.95
    skill_gated_stations: List[str] = None
    def __post_init__(self):
        if self.skill_gated_stations is None:
            self.skill_gated_stations = ["KNYC","KLAX","KMDW","KBOS","KATL","KSFO","KSEA"]
class RiskManager:
    def __init__(self, config: Optional[RiskConfig] = None):
        self.config = config or RiskConfig()
        self.daily_pnl = 0.0
        self.drawdown_pct = 0.0
        self.consecutive_losses = 0
        self.kill_switch_active = False
        self.kill_reason = ""
    def is_station_allowed(self, station): return station in self.config.skill_gated_stations
    def check_daily_loss(self):
        if self.daily_pnl <= -self.config.max_daily_loss:
            self.kill_switch_active = True
            self.kill_reason = f"Daily loss: ${abs(self.daily_pnl):.2f}"
            return False
        return True
    def check_drawdown(self):
        if self.drawdown_pct >= self.config.max_drawdown_pct:
            self.kill_switch_active = True
            self.kill_reason = f"Drawdown: {self.drawdown_pct:.1f}%"
            return False
        return True
    def check_consecutive_losses(self):
        if self.consecutive_losses >= self.config.consecutive_loss_limit:
            self.kill_switch_active = True
            self.kill_reason = f"{self.consecutive_losses} losses"
            return False
        return True
    def update_after_trade(self, pnl):
        self.daily_pnl += pnl
        if pnl < 0: self.consecutive_losses += 1
        else: self.consecutive_losses = 0
    def risk_report(self):
        return {"daily_pnl": round(self.daily_pnl,2), "drawdown_pct": round(self.drawdown_pct,2), "consecutive_losses": self.consecutive_losses, "kill_switch_active": self.kill_switch_active, "kill_reason": self.kill_reason, "allowed_stations": self.config.skill_gated_stations}
risk_manager = RiskManager()
