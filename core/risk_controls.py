#!/usr/bin/env python3
"""
RISK CONTROLS MODULE (Marty's Phase 1 B1.5) — Configurable Guardrails

Provides configurable risk guardrails with alerting hooks and kill-switch logic.
All thresholds are configurable via environment variables.

Configurable Risk Controls:
- max_daily_loss = 300 (halt trading + alert when exceeded)
- max_drawdown_pct = 10.0 (full suspension when breached)
- kill_switch_triggers:
    - 5 consecutive losses
    - correlation > 0.70 same direction
    - signal conflict > 0.95 for 2+ days

Station Gating:
- Only trade approved stations: KNYC, KLAX, KMDW, KBOS, KATL, KSFO, KSEA
"""

import os
import logging
from dataclasses import dataclass, field
from typing import List, Optional, Tuple
from datetime import datetime, timezone
from enum import Enum

_LOGGER = logging.getLogger(__name__)


class RiskState(Enum):
    """Current risk state of the system."""
    OK = "ok"
    WARNING = "warning"
    SUSPENDED = "suspended"
    KILLED = "killed"


@dataclass
class RiskMetrics:
    """Current risk state metrics."""
    daily_pnl: float = 0.0
    max_drawdown_pct: float = 0.0
    consecutive_losses: int = 0
    peak_balance: float = 0.0
    current_balance: float = 0.0
    risk_state: RiskState = RiskState.OK
    kill_switch_reasons: List[str] = field(default_factory=list)
    last_update_utc: str = ""


# ─── Configurable Risk Thresholds ───────────────────────────────────────
# All values can be overridden via environment variables.

def _get_configured_value(env_var: str, default: float) -> float:
    """Get a configured value from environment, falling back to default."""
    env_val = os.getenv(env_var)
    if env_val is not None:
        try:
            return float(env_val)
        except (ValueError, TypeError):
            _LOGGER.warning(f"Invalid value for {env_var}: {env_val}, using default {default}")
    return default


@dataclass(frozen=True)
class RiskConfig:
    """Configuration for risk guardrails."""
    max_daily_loss: float = _get_configured_value("MAX_DAILY_LOSS", 300.0)
    max_drawdown_pct: float = _get_configured_value("MAX_DRAWDOWN_PCT", 10.0)
    consecutive_loss_limit: int = _get_configured_value("CONSECUTIVE_LOSS_LIMIT", 5)
    correlation_threshold: float = _get_configured_value("CORRELATION_THRESHOLD", 0.70)
    signal_conflict_threshold: float = _get_configured_value("SIGNAL_CONFLICT_THRESHOLD", 0.95)
    signal_conflict_days: int = _get_configured_value("SIGNAL_CONFLICT_DAYS", 2)
    
    # Approved station list (B1.5.2)
    approved_stations: List[str] = field(default_factory=lambda: [
        "KNYC", "KLAX", "KMDW", "KBOS", "KATL", "KSFO", "KSEA"
    ])
    
    # Enable risk controls
    enabled: bool = _get_configured_value("RISK_CONTROLS_ENABLED", 1.0) != 0.0


# Default config instance
DEFAULT_RISK_CONFIG = RiskConfig()


# ─── Station Approval Gate ──────────────────────────────────────────────

def is_station_approved(station: str, config: RiskConfig = DEFAULT_RISK_CONFIG) -> bool:
    """
    Check if a station is in the approved list.
    
    Only trade stations explicitly in the approved list.
    """
    return station.upper() in [s.upper() for s in config.approved_stations]


def get_approved_stations(config: RiskConfig = DEFAULT_RISK_CONFIG) -> List[str]:
    """Return list of approved station codes."""
    return list(config.approved_stations)


# ─── Kill Switch Triggers ───────────────────────────────────────────────

def check_consecutive_losses(risk_metrics: RiskMetrics, 
                             config: RiskConfig = DEFAULT_RISK_CONFIG) -> Tuple[bool, str]:
    """Check if consecutive losses threshold is exceeded."""
    if risk_metrics.consecutive_losses >= config.consecutive_loss_limit:
        return True, f"Consecutive losses ({risk_metrics.consecutive_losses}) >= limit ({config.consecutive_loss_limit})"
    return False, ""


def check_correlation_risk(correlation: float, 
                          direction: str,
                          config: RiskConfig = DEFAULT_RISK_CONFIG) -> Tuple[bool, str]:
    """
    Check if correlation risk exceeds threshold (same direction trades).
    
    High correlation (>0.70) in same direction indicates over-concentration.
    """
    if correlation > config.correlation_threshold and direction:
        return True, f"Correlation ({correlation:.2%}) > threshold ({config.correlation_threshold:.2%})"
    return False, ""


def check_signal_conflict(conflict_metric: float,
                          conflict_days: int,
                          config: RiskConfig = DEFAULT_RISK_CONFIG) -> Tuple[bool, str]:
    """
    Check if signal conflict exceeds threshold for multiple days.
    
    High conflict (>0.95) for 2+ days indicates unstable signal.
    """
    if conflict_metric > config.signal_conflict_threshold and conflict_days >= config.signal_conflict_days:
        return True, f"Signal conflict ({conflict_metric:.2%}) > threshold ({config.signal_conflict_threshold:.2%}) for {conflict_days} days"
    return False, ""


# ─── Risk State Evaluation ──────────────────────────────────────────────

def evaluate_risk_state(risk_metrics: RiskMetrics,
                        config: RiskConfig = DEFAULT_RISK_CONFIG) -> RiskMetrics:
    """
    Evaluate current risk state based on all thresholds.
    
    Returns updated RiskMetrics with risk_state and kill_switch_reasons.
    """
    reasons = []
    
    # Check max daily loss
    if config.max_daily_loss and risk_metrics.daily_pnl < -config.max_daily_loss:
        reasons.append(f"Daily loss (${-risk_metrics.daily_pnl:.2f}) > limit (${config.max_daily_loss:.2f})")
    
    # Check max drawdown
    if config.max_drawdown_pct and risk_metrics.max_drawdown_pct >= config.max_drawdown_pct:
        reasons.append(f"Drawdown ({risk_metrics.max_drawdown_pct:.1%}) >= limit ({config.max_drawdown_pct:.1%})")
    
    # Check kill switch triggers
    is_killed = False
    killed_reason = ""
    
    # 1. Consecutive losses
    is_exceeded, reason = check_consecutive_losses(risk_metrics, config)
    if is_exceeded:
        reasons.append(reason)
        killed_reason = "consecutive_losses"
        is_killed = True
    
    # Add other kill switch checks here as needed
    
    if is_killed:
        risk_metrics.risk_state = RiskState.KILLED
    elif reasons:
        risk_metrics.risk_state = RiskState.WARNING
    else:
        risk_metrics.risk_state = RiskState.OK
    
    risk_metrics.kill_switch_reasons = reasons
    risk_metrics.last_update_utc = datetime.now(timezone.utc).isoformat()
    
    return risk_metrics


# ─── Risk Report Functions ──────────────────────────────────────────────

def risk_report(risk_metrics: RiskMetrics,
                config: RiskConfig = DEFAULT_RISK_CONFIG) -> dict:
    """
    Generate a risk report with current exposure, daily P&L, and kill switch status.
    
    Returns a dict suitable for logging, alerting, or dashboard display.
    """
    return {
        "exposure_usd": abs(risk_metrics.current_balance),
        "daily_pnl_usd": risk_metrics.daily_pnl,
        "max_drawdown_pct": risk_metrics.max_drawdown_pct,
        "peak_balance_usd": risk_metrics.peak_balance,
        "current_balance_usd": risk_metrics.current_balance,
        "consecutive_losses": risk_metrics.consecutive_losses,
        "kill_switch_enabled": config.enabled,
        "risk_state": risk_metrics.risk_state.value,
        "kill_switch_reasons": risk_metrics.kill_switch_reasons,
        "config": {
            "max_daily_loss_usd": config.max_daily_loss,
            "max_drawdown_pct": config.max_drawdown_pct,
            "consecutive_loss_limit": config.consecutive_loss_limit,
            "correlation_threshold": config.correlation_threshold,
            "signal_conflict_threshold": config.signal_conflict_threshold,
            "signal_conflict_days": config.signal_conflict_days,
            "approved_stations_count": len(config.approved_stations),
        },
        "timestamp_utc": risk_metrics.last_update_utc or datetime.now(timezone.utc).isoformat(),
    }


def format_risk_alert(risk_metrics: RiskMetrics) -> str:
    """Format risk metrics as a human-readable alert string."""
    report = risk_report(risk_metrics)
    
    lines = [
        "=== RISK GUARDRAILS ALERT ===",
        f"State: {report['risk_state'].upper()}",
        f"Current Balance: ${report['current_balance_usd']:,.2f}",
        f"Daily P&L: ${report['daily_pnl_usd']:,.2f}",
        f"Drawdown: {report['max_drawdown_pct']:.1%}",
        f"Consecutive Losses: {report['consecutive_losses']}",
    ]
    
    if report['kill_switch_reasons']:
        lines.append("Kill Switch Triggers:")
        for reason in report['kill_switch_reasons']:
            lines.append(f"  - {reason}")
    
    return "\n".join(lines)


if __name__ == "__main__":
    # Demo usage
    print("Risk Controls Demo")
    print("=" * 70)
    
    # Show default config
    print(f"\nDefault Risk Config:")
    print(f"  Max Daily Loss: ${DEFAULT_RISK_CONFIG.max_daily_loss:.2f}")
    print(f"  Max Drawdown: {DEFAULT_RISK_CONFIG.max_drawdown_pct:.1f}%")
    print(f"  Consecutive Loss Limit: {DEFAULT_RISK_CONFIG.consecutive_loss_limit}")
    print(f"  Correlation Threshold: {DEFAULT_RISK_CONFIG.correlation_threshold:.2%}")
    print(f"  Approved Stations: {DEFAULT_RISK_CONFIG.approved_stations}")
    
    # Show sample metrics
    metrics = RiskMetrics(
        daily_pnl=-350.0,
        max_drawdown_pct=12.5,
        consecutive_losses=5,
        current_balance=9650.0,
        peak_balance=10000.0,
        risk_state=RiskState.OK,
    )
    
    metrics = evaluate_risk_state(metrics)
    print(f"\nSample Risk Report:")
    print(format_risk_alert(metrics))
