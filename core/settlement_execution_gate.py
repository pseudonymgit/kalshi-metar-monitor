#!/usr/bin/env python3
"""
A6 — Settlement Execution Gate

Checks whether a settlement epoch is actually tradeable before signaling
a reversion trade. Prevents trades when:

1. Market is closed (weekend/holiday)
2. Settlement window is not active (too early or too late)
3. Station-level cooldown is active (recent alert for same station)
4. Boundary-level cooldown is active (recent alert for crossing boundary)
5. Station is not in the approved list

Usage:
    from core.settlement_execution_gate import (
        SettlementExecutionGate,
        GateResult,
        GateVerdict,
    )

    gate = SettlementExecutionGate()
    result = gate.evaluate(
        station="KATL",
        trading_date="2026-07-24",
        epoch_id="2026-07-24_HIGH",
        station_last_emit_ts=None,  # or timestamp in epoch seconds
        boundary_last_emit_ts=None,
        approved_stations=["KATL", "KBOS", ...],
    )
    if result.verdict == GateVerdict.PASS:
        # Proceed with trade
    else:
        # Trade blocked — check result.reason
"""

from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple
from enum import Enum

# ─── Verdict Enum ──────────────────────────────────────────────

class GateVerdict(Enum):
    """Verdict from the settlement execution gate."""
    PASS = "pass"                            # All checks pass — tradeable
    MARKET_CLOSED = "market_closed"          # Not a trading day
    BEFORE_WINDOW = "before_entry_window"    # Too early to trade
    AFTER_WINDOW = "after_entry_window"      # Too late to trade
    STATION_COOLDOWN = "station_cooldown"    # Station-level cooldown active
    BOUNDARY_COOLDOWN = "boundary_cooldown"  # Boundary-level cooldown active
    STATION_NOT_APPROVED = "station_not_approved"  # Not in approved list
    MARKET_SETTLED = "market_already_settled"  # Market has already settled
    SETTLEMENT_UNKNOWN = "settlement_time_unknown"  # Cannot determine settlement time

    def is_pass(self) -> bool:
        return self == GateVerdict.PASS

    def is_blocked(self) -> bool:
        return not self.is_pass()


# ─── Config Defaults ───────────────────────────────────────────

# Cooldown periods (seconds) — matching metar_monitor.py values
DEFAULT_STATION_COOLDOWN_SECONDS = 300    # 5min
DEFAULT_BOUNDARY_COOLDOWN_SECONDS = 900  # 15min

# Entry window: T-18h to T-2h before settlement
ENTRY_WINDOW_HOURS_BEFORE_CLOSE = 2.0    # 2h before settlement
ENTRY_WINDOW_HOURS_AFTER_OPEN = 18.0     # 18h before settlement


# ─── Result ────────────────────────────────────────────────────

class GateResult:
    """
    Result of a settlement execution gate evaluation.

    Attributes:
        verdict: The gate verdict
        reason: Human-readable reason string
        details: Dict of individual check results for diagnostics
        remaining_cooldown_seconds: Seconds remaining in cooldown (if applicable)
    """

    def __init__(
        self,
        verdict: GateVerdict,
        reason: str = "",
        details: Optional[Dict[str, Any]] = None,
        remaining_cooldown_seconds: float = 0.0,
    ):
        self.verdict = verdict
        self.reason = reason or verdict.value
        self.details = details or {}
        self.remaining_cooldown_seconds = remaining_cooldown_seconds

    def to_dict(self) -> Dict[str, Any]:
        return {
            "verdict": self.verdict.value,
            "pass": self.verdict.is_pass(),
            "reason": self.reason,
            "remaining_cooldown_seconds": round(self.remaining_cooldown_seconds, 1),
            "details": self.details,
        }

    def __repr__(self) -> str:
        return f"<GateResult {self.verdict.value}: {self.reason}>"


# ─── Gate ──────────────────────────────────────────────────────

class SettlementExecutionGate:
    """
    Gate that checks whether a settlement epoch is tradeable.

    Evaluates:
    1. Market is open (trading day)
    2. Settlement window is active
    3. No station-level cooldown
    4. No boundary-level cooldown
    5. Station is approved
    """

    def __init__(
        self,
        station_cooldown_seconds: int = DEFAULT_STATION_COOLDOWN_SECONDS,
        boundary_cooldown_seconds: int = DEFAULT_BOUNDARY_COOLDOWN_SECONDS,
    ):
        self.station_cooldown_seconds = station_cooldown_seconds
        self.boundary_cooldown_seconds = boundary_cooldown_seconds

        # Try to import kalshi_calendar for trading day checks
        try:
            from core.station_time import is_trading_day, settlement_time_utc, is_within_entry_window
            self._is_trading_day = is_trading_day
            self._settlement_time_utc = settlement_time_utc
            self._is_within_entry_window = is_within_entry_window
        except ImportError:
            self._is_trading_day = None
            self._settlement_time_utc = None
            self._is_within_entry_window = None

    def evaluate(
        self,
        station: str,
        trading_date: str,
        epoch_id: Optional[str] = None,
        station_last_emit_ts: Optional[float] = None,
        boundary_last_emit_ts: Optional[float] = None,
        approved_stations: Optional[List[str]] = None,
        now_utc: Optional[datetime] = None,
    ) -> GateResult:
        """
        Evaluate whether a settlement epoch is tradeable.

        Args:
            station: Station code (e.g. 'KATL')
            trading_date: Local trading date YYYY-MM-DD
            epoch_id: Optional epoch identifier for diagnostics
            station_last_emit_ts: Unix timestamp of last station-level alert emit
            boundary_last_emit_ts: Unix timestamp of last boundary-level alert emit
            approved_stations: List of approved stations (None = all approved)
            now_utc: Current UTC time (defaults to now)

        Returns:
            GateResult with verdict and details
        """
        if now_utc is None:
            now_utc = datetime.now(timezone.utc)

        now_epoch = now_utc.timestamp()
        details: Dict[str, Any] = {
            "station": station,
            "trading_date": trading_date,
            "epoch_id": epoch_id,
            "now_utc": now_utc.isoformat(),
            "checks": {},
        }

        # ── Check 1: Station Approved ─────────────────────────
        if approved_stations is not None and station not in approved_stations:
            details["checks"]["station_approved"] = {
                "pass": False,
                "station": station,
                "approved_stations": approved_stations,
            }
            return GateResult(
                verdict=GateVerdict.STATION_NOT_APPROVED,
                reason=f"Station {station} not in approved list",
                details=details,
            )
        details["checks"]["station_approved"] = {"pass": True}

        # ── Check 2: Settlement Time Known ────────────────────
        if self._settlement_time_utc is None:
            # Fallback: basic check
            pass
        else:
            settlement = self._settlement_time_utc(station, trading_date)
            if settlement is None:
                details["checks"]["settlement_time"] = {
                    "pass": False,
                    "reason": "settlement_time_unknown",
                }
                return GateResult(
                    verdict=GateVerdict.SETTLEMENT_UNKNOWN,
                    reason=f"Cannot determine settlement time for {station} on {trading_date}",
                    details=details,
                )
            details["checks"]["settlement_time"] = {
                "pass": True,
                "settlement_utc": settlement.isoformat(),
            }

        # ── Check 3: Trading Day ──────────────────────────────
        if self._is_trading_day is not None:
            is_open = self._is_trading_day(datetime.strptime(trading_date, "%Y-%m-%d"))
            details["checks"]["market_open"] = {
                "pass": is_open,
                "trading_date": trading_date,
            }
            if not is_open:
                return GateResult(
                    verdict=GateVerdict.MARKET_CLOSED,
                    reason=f"Market closed on {trading_date} (weekend/holiday)",
                    details=details,
                )
        else:
            # Fallback: basic weekday check
            trade_date = datetime.strptime(trading_date, "%Y-%m-%d")
            is_open = trade_date.weekday() < 5
            details["checks"]["market_open"] = {
                "pass": is_open,
                "trading_date": trading_date,
                "fallback_weekday_only": True,
            }
            if not is_open:
                return GateResult(
                    verdict=GateVerdict.MARKET_CLOSED,
                    reason=f"Market closed on {trading_date} (weekend)",
                    details=details,
                )

        # ── Check 4: Entry Window ─────────────────────────────
        if self._is_within_entry_window is not None:
            within_window, window_reason = self._is_within_entry_window(
                station, trading_date, now_utc
            )
            details["checks"]["entry_window"] = {
                "pass": within_window,
                "reason": window_reason,
            }
            if not within_window:
                if "settled" in window_reason.lower():
                    verdict = GateVerdict.MARKET_SETTLED
                elif "close" in window_reason.lower() or "late" in window_reason.lower():
                    verdict = GateVerdict.AFTER_WINDOW
                else:
                    verdict = GateVerdict.BEFORE_WINDOW
                return GateResult(
                    verdict=verdict,
                    reason=window_reason,
                    details=details,
                )
        else:
            details["checks"]["entry_window"] = {"pass": True, "fallback": True}

        # ── Check 5: Station Cooldown ─────────────────────────
        if station_last_emit_ts is not None:
            seconds_since_station = now_epoch - station_last_emit_ts
            station_cooldown_active = seconds_since_station < self.station_cooldown_seconds
            remaining_station = max(0.0, self.station_cooldown_seconds - seconds_since_station)
            details["checks"]["station_cooldown"] = {
                "pass": not station_cooldown_active,
                "active": station_cooldown_active,
                "seconds_since_last_emit": round(seconds_since_station, 1),
                "cooldown_seconds": self.station_cooldown_seconds,
                "remaining_seconds": round(remaining_station, 1),
            }
            if station_cooldown_active:
                return GateResult(
                    verdict=GateVerdict.STATION_COOLDOWN,
                    reason=f"Station cooldown active for {station}: {remaining_station:.0f}s remaining",
                    details=details,
                    remaining_cooldown_seconds=remaining_station,
                )
        else:
            details["checks"]["station_cooldown"] = {
                "pass": True,
                "last_emit": None,
                "note": "No prior station emit — cooldown not applicable",
            }

        # ── Check 6: Boundary Cooldown ────────────────────────
        if boundary_last_emit_ts is not None:
            seconds_since_boundary = now_epoch - boundary_last_emit_ts
            boundary_cooldown_active = seconds_since_boundary < self.boundary_cooldown_seconds
            remaining_boundary = max(0.0, self.boundary_cooldown_seconds - seconds_since_boundary)
            details["checks"]["boundary_cooldown"] = {
                "pass": not boundary_cooldown_active,
                "active": boundary_cooldown_active,
                "seconds_since_last_emit": round(seconds_since_boundary, 1),
                "cooldown_seconds": self.boundary_cooldown_seconds,
                "remaining_seconds": round(remaining_boundary, 1),
            }
            if boundary_cooldown_active:
                return GateResult(
                    verdict=GateVerdict.BOUNDARY_COOLDOWN,
                    reason=f"Boundary cooldown active: {remaining_boundary:.0f}s remaining",
                    details=details,
                    remaining_cooldown_seconds=remaining_boundary,
                )
        else:
            details["checks"]["boundary_cooldown"] = {
                "pass": True,
                "last_emit": None,
                "note": "No prior boundary emit — cooldown not applicable",
            }

        # ── All Checks Pass ───────────────────────────────────
        return GateResult(
            verdict=GateVerdict.PASS,
            reason="All checks pass — epoch is tradeable",
            details=details,
        )

    def evaluate_from_tracker(
        self,
        station: str,
        trading_date: str,
        tracker: Dict[str, Any],
        approved_stations: Optional[List[str]] = None,
        now_utc: Optional[datetime] = None,
    ) -> GateResult:
        """
        Evaluate from a spike tracker dict (metar_monitor format).

        Extracts station_last_emit_ts and boundary_last_emit_ts from the
        tracker if available.

        Args:
            station: Station code
            trading_date: Trading date YYYY-MM-DD
            tracker: Spike epoch tracker dict
            approved_stations: Optional approved station list
            now_utc: Current UTC time

        Returns:
            GateResult
        """
        epoch_id = tracker.get("epoch_id", f"{trading_date}_{station}")
        stationary_cooldown = tracker.get("station_cooldown_seconds")
        boundary_cooldown = tracker.get("boundary_cooldown_seconds")

        gate = self
        if stationary_cooldown is not None:
            gate = SettlementExecutionGate(
                station_cooldown_seconds=int(stationary_cooldown),
                boundary_cooldown_seconds=int(boundary_cooldown or self.boundary_cooldown_seconds),
            )

        return gate.evaluate(
            station=station,
            trading_date=trading_date,
            epoch_id=epoch_id,
            station_last_emit_ts=tracker.get("_station_last_emit_ts"),
            boundary_last_emit_ts=tracker.get("_boundary_last_emit_ts"),
            approved_stations=approved_stations,
            now_utc=now_utc,
        )


# ─── Self-Test ──────────────────────────────────────────────────

def _self_test():
    """Run basic validation of the settlement execution gate."""
    gate = SettlementExecutionGate()

    # Test 1: Weekend — market closed
    r1 = gate.evaluate(
        station="KATL",
        trading_date="2026-07-26",  # Sunday
    )
    assert r1.verdict == GateVerdict.MARKET_CLOSED, f"Expected MARKET_CLOSED, got {r1.verdict}"
    print(f"  Test 1 PASS: {r1}")

    # Test 2: Weekday — should pass basic checks
    r2 = gate.evaluate(
        station="KATL",
        trading_date="2026-07-24",  # Friday
        approved_stations=["KATL", "KBOS"],
    )
    assert r2.verdict == GateVerdict.PASS, f"Expected PASS, got {r2.verdict} ({r2.reason})"
    print(f"  Test 2 PASS: {r2}")

    # Test 3: Station not approved
    r3 = gate.evaluate(
        station="KHOU",
        trading_date="2026-07-24",
        approved_stations=["KATL", "KBOS"],
    )
    assert r3.verdict == GateVerdict.STATION_NOT_APPROVED, f"Expected STATION_NOT_APPROVED, got {r3.verdict}"
    print(f"  Test 3 PASS: {r3}")

    # Test 4: Station cooldown active
    r4 = gate.evaluate(
        station="KATL",
        trading_date="2026-07-24",
        station_last_emit_ts=datetime.now(timezone.utc).timestamp() - 60,  # 60s ago
    )
    assert r4.verdict == GateVerdict.STATION_COOLDOWN, f"Expected STATION_COOLDOWN, got {r4.verdict}"
    assert r4.remaining_cooldown_seconds > 0, f"Expected positive remaining cooldown"
    print(f"  Test 4 PASS: {r4}")

    # Test 5: Station cooldown expired
    r5 = gate.evaluate(
        station="KATL",
        trading_date="2026-07-24",
        station_last_emit_ts=datetime.now(timezone.utc).timestamp() - 600,  # 10min ago
    )
    assert r5.verdict == GateVerdict.PASS, f"Expected PASS, got {r5.verdict}"
    print(f"  Test 5 PASS: {r5}")

    # Test 6: Boundary cooldown active
    r6 = gate.evaluate(
        station="KATL",
        trading_date="2026-07-24",
        boundary_last_emit_ts=datetime.now(timezone.utc).timestamp() - 120,  # 2min ago
    )
    assert r6.verdict == GateVerdict.BOUNDARY_COOLDOWN, f"Expected BOUNDARY_COOLDOWN, got {r6.verdict}"
    print(f"  Test 6 PASS: {r6}")

    # Test 7: Both cooldowns active — station cooldown should block first
    r7 = gate.evaluate(
        station="KATL",
        trading_date="2026-07-24",
        station_last_emit_ts=datetime.now(timezone.utc).timestamp() - 60,  # 60s ago
        boundary_last_emit_ts=datetime.now(timezone.utc).timestamp() - 120,  # 2min ago
    )
    assert r7.verdict == GateVerdict.STATION_COOLDOWN, f"Expected STATION_COOLDOWN, got {r7.verdict}"
    print(f"  Test 7 PASS: {r7}")

    # Test 8: No cooldowns, all pass
    r8 = gate.evaluate(
        station="KATL",
        trading_date="2026-07-24",
        approved_stations=["KATL", "KBOS", "KLAX", "KSEA", "KATL"],
    )
    assert r8.verdict == GateVerdict.PASS, f"Expected PASS, got {r8.verdict}"
    assert r8.verdict.is_pass()
    assert not r8.verdict.is_blocked()
    print(f"  Test 8 PASS: {r8}")

    # Test 9: to_dict
    d = r8.to_dict()
    assert d["pass"] is True
    assert d["verdict"] == "pass"
    assert "details" in d
    print(f"  Test 9 PASS: to_dict={d['verdict']}")

    print("\nAll self-tests PASS")
    return True


if __name__ == "__main__":
    _self_test()