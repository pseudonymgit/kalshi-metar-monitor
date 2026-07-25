"""
Operation State Module — Graceful Degradation State Machine (v1.0 — 2026-07-23)

Defines the system operation states: NORMAL, WARN, DEGRADED, HALTED, EMERGENCY.
Each state defines:
  - Signal pipeline behavior (which signals run, at what weight)
  - Position sizing multiplier (fraction of normal sizing)
  - Alert intensity (verbosity, frequency, destinations)
  - Allowed transitions

Usage:
    from core.operation_state import OperationState, get_state, set_state
    
    # Get current state
    state = get_state()
    
    # Check if we should reduce position sizing
    multiplier = state.position_sizing_multiplier()
    
    # Check if a signal should be suppressed
    if state.should_suppress_signal(signal_name, station):
        continue  # Skip this signal
    
    # Transition to a new state
    set_state(OperationState.DEGRADED, reason="3+ stations stale for >30min")
"""

from enum import Enum
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set

import threading


# ─── State Definition ────────────────────────────────────────────────────────


class OperationState(Enum):
    """
    System operation states with defined behavior profiles.
    
    State progression:
        NORMAL → WARN → DEGRADED → HALTED
        NORMAL → EMERGENCY (direct, for critical failures)
        Any state → HALTED (for abort conditions)
        HALTED → NORMAL (only via manual intervention + halt file clear)
    """
    
    NORMAL = "normal"
    WARN = "warn"
    DEGRADED = "degraded"
    HALTED = "halted"
    EMERGENCY = "emergency"
    
    # ─── Position Sizing Multiplier ──────────────────────────────────────
    
    def position_sizing_multiplier(self) -> float:
        """
        Position sizing multiplier for each state.
        
        - NORMAL: 1.0 (full normal sizing)
        - WARN: 0.75 (75% of normal sizing)
        - DEGRADED: 0.50 (50% of normal sizing)
        - HALTED: 0.0 (no new positions)
        - EMERGENCY: 0.0 (no new positions, close existing)
        """
        return {
            OperationState.NORMAL: 1.0,
            OperationState.WARN: 0.75,
            OperationState.DEGRADED: 0.50,
            OperationState.HALTED: 0.0,
            OperationState.EMERGENCY: 0.0,
        }[self]
    
    # ─── Alert Intensity ─────────────────────────────────────────────────
    
    def alert_intensity(self) -> str:
        """
        Alert verbosity/intensity level.
        
        - NORMAL: "normal" (standard alerts, no duplicates)
        - WARN: "elevated" (more verbose, include diagnostic context)
        - DEGRADED: "high" (frequent status updates, every cycle)
        - HALTED: "minimal" (only halt-related alerts)
        - EMERGENCY: "critical" (only emergency-level alerts)
        """
        return {
            OperationState.NORMAL: "normal",
            OperationState.WARN: "elevated",
            OperationState.DEGRADED: "high",
            OperationState.HALTED: "minimal",
            OperationState.EMERGENCY: "critical",
        }[self]
    
    def alert_throttle_multiplier(self) -> float:
        """
        Alert throttle multiplier (higher = less throttling, more alerts).
        
        - NORMAL: 1.0 (standard throttling)
        - WARN: 1.5 (50% more alerts for diagnostics)
        - DEGRADED: 2.0 (2x alerts for visibility)
        - HALTED: 0.2 (mostly quiet)
        - EMERGENCY: 1.0 (standard but critical-only)
        """
        return {
            OperationState.NORMAL: 1.0,
            OperationState.WARN: 1.5,
            OperationState.DEGRADED: 2.0,
            OperationState.HALTED: 0.2,
            OperationState.EMERGENCY: 1.0,
        }[self]
    
    # ─── Signal Pipeline Behavior ─────────────────────────────────────────
    
    def should_suppress_signal(
        self,
        signal_name: str,
        station: Optional[str] = None,
    ) -> bool:
        """
        Determine whether a signal should be suppressed in the current state.
        
        Args:
            signal_name: Name of the signal (e.g., "forecast_disagreement")
            station: Optional station ICAO code for per-station gating
            
        Returns:
            True if the signal should be suppressed (not run)
        """
        if self == OperationState.NORMAL:
            return False
        elif self == OperationState.WARN:
            # In WARN state, suppress only non-essential signals
            non_essential = {
                "late_day_momentum",
                "spread_momentum",
                "round_number_anchoring",
            }
            return signal_name in non_essential
        elif self == OperationState.DEGRADED:
            # In DEGRADED state, suppress all but core signals
            core_signals = {
                "forecast_disagreement",
                "temperature_advection",
                "nwp_analog",
                "hrrr_bias_corrected",
            }
            return signal_name not in core_signals
        elif self in (OperationState.HALTED, OperationState.EMERGENCY):
            # In HALTED/EMERGENCY, suppress all signals
            return True
        return False
    
    def position_sizing_cap(self) -> Optional[float]:
        """
        Maximum position size as fraction of normal max.
        None means no cap beyond the multiplier.
        """
        return {
            OperationState.NORMAL: None,
            OperationState.WARN: None,
            OperationState.DEGRADED: 0.75,  # Cap at 75% of max even for high-conviction
            OperationState.HALTED: 0.0,
            OperationState.EMERGENCY: 0.0,
        }[self]
    
    # ─── State Transition Validation ──────────────────────────────────────
    
    def can_transition_to(self, target: "OperationState") -> bool:
        """
        Validate whether a transition to the target state is allowed.
        
        Rules:
        - NORMAL ↔ WARN (bidirectional)
        - NORMAL → DEGRADED (escalation)
        - NORMAL → EMERGENCY (direct for critical failures)
        - WARN → DEGRADED (escalation)
        - WARN → NORMAL (de-escalation)
        - DEGRADED → HALTED (escalation)
        - DEGRADED → WARN (de-escalation)
        - HALTED → NORMAL (manual reset only)
        - EMERGENCY → HALTED (de-escalation from emergency)
        - EMERGENCY → NORMAL (if resolved, via manual intervention)
        """
        allowed = {
            OperationState.NORMAL: {
                OperationState.WARN,
                OperationState.DEGRADED,
                OperationState.EMERGENCY,
            },
            OperationState.WARN: {
                OperationState.NORMAL,
                OperationState.DEGRADED,
            },
            OperationState.DEGRADED: {
                OperationState.WARN,
                OperationState.HALTED,
            },
            OperationState.HALTED: {
                OperationState.NORMAL,  # Manual reset
            },
            OperationState.EMERGENCY: {
                OperationState.HALTED,
                OperationState.NORMAL,  # If resolved
            },
        }
        return target in allowed.get(self, set())


# ─── State Manager ───────────────────────────────────────────────────────────


class _StateManager:
    """
    Thread-safe operation state manager.
    
    Tracks:
    - Current state
    - State history (last N transitions with timestamps)
    - Transition reason
    """
    
    def __init__(self, initial_state: OperationState = OperationState.NORMAL):
        self._lock = threading.Lock()
        self._current_state = initial_state
        self._history: List[Dict] = []
        self._reason = "Initial startup"
        self._max_history = 100
        
    @property
    def current_state(self) -> OperationState:
        with self._lock:
            return self._current_state
    
    @property
    def reason(self) -> str:
        with self._lock:
            return self._reason
    
    def transition_to(
        self,
        target: OperationState,
        reason: str = "",
        force: bool = False,
    ) -> bool:
        """
        Transition to a new state.
        
        Args:
            target: Target state
            reason: Reason for the transition
            force: If True, skip validation check
            
        Returns:
            True if transition was successful, False if not allowed
        """
        with self._lock:
            source = self._current_state
            if not force and not source.can_transition_to(target):
                return False
            
            self._current_state = target
            self._reason = reason
            
            entry = {
                "timestamp_utc": datetime.now(timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
                "from": source.value,
                "to": target.value,
                "reason": reason,
                "forced": force,
            }
            self._history.append(entry)
            
            # Trim history
            if len(self._history) > self._max_history:
                self._history = self._history[-self._max_history:]
            
            return True
    
    def get_history(self, limit: int = 10) -> List[Dict]:
        """Get the last N state transitions."""
        with self._lock:
            return list(self._history[-limit:])
    
    def get_summary(self) -> Dict:
        """Get a summary of current state and recent history."""
        with self._lock:
            return {
                "current_state": self._current_state.value,
                "reason": self._reason,
                "position_sizing_multiplier": (
                    self._current_state.position_sizing_multiplier()
                ),
                "alert_intensity": self._current_state.alert_intensity(),
                "recent_transitions": [
                    {
                        "ts": h["timestamp_utc"],
                        "from": h["from"],
                        "to": h["to"],
                        "reason": h["reason"],
                    }
                    for h in self._history[-5:]
                ],
            }


# ─── Module-Level Singleton ──────────────────────────────────────────────────

_manager = _StateManager()


def get_state() -> OperationState:
    """Get the current operation state."""
    return _manager.current_state


def set_state(
    state: OperationState,
    reason: str = "",
    force: bool = False,
) -> bool:
    """
    Transition to a new operation state.
    
    Args:
        state: Target state
        reason: Reason for the transition
        force: Bypass transition validation
        
    Returns:
        True if transition succeeded
    """
    return _manager.transition_to(state, reason=reason, force=force)


def get_state_summary() -> Dict:
    """Get summary of current state and history."""
    return _manager.get_summary()


def get_state_history(limit: int = 10) -> List[Dict]:
    """Get recent state transitions."""
    return _manager.get_history(limit=limit)


def reset_state(reason: str = "Manual reset") -> bool:
    """Reset to NORMAL state (for manual intervention)."""
    return _manager.transition_to(
        OperationState.NORMAL, reason=reason, force=True
    )