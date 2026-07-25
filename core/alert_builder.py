#!/usr/bin/env python3
"""
Paper Trading Alert Builder/Renderer v2.1 (B-MODE v2)
Slim Discord embed layout with S/A/B/C/D/F Opportunity Grade + Edge calculation
Hard filtering applied before alert emission.
"""

from enum import Enum
from typing import Dict, Any, Tuple, Optional
import os
import time
import logging
import re

# Instance environment variable (PROD/DEV/SBOX)
INSTANCE = os.getenv("PAPER_TRADING_INSTANCE", "DEV").upper()


def generate_market_url(station: str, market_type: str, strike: Optional[str] = None) -> str:
    """
    Generate Kalshi market URLs following the pattern: `https://kalshi.com/markets/{series_ticker}`
    
    Series ticker format: `KX{HIGH|LOW}{CODE}` where CODE is Kalshi station code (e.g., TATL for KATL)
    This function converts station+market info to the proper URL format.
    
    Args:
        station: Station ICAO code (e.g., 'KATL', 'KLAX')
        market_type: Market type ('HIGH' or 'LOW')
        strike: Optional strike price to append or other parameters
    
    Returns:
        Kalshi market URL string
    """
    # Import station registry only when needed to avoid circular imports
    try:
        from .station_registry import get_kalshi_station_code_for_icao 
        kalshi_code = get_kalshi_station_code_for_icao(station)
    except ImportError:
        # Fallback mapping if station registry unavailable
        station_code = station.upper().replace('K', '')  # Remove K prefix to get basic code
        kalshi_code = station_code
    
    # Ensure market type is uppercase
    market_type_upper = market_type.upper()
    
    # Build series ticker in the format: WXHIGH{STATION}|WXLOW{STATION} or similar
    # The pattern is KX{TYPE}{STATION_CODE} in the description, which for weather would be
    series_ticker = f"WX{market_type_upper}{kalshi_code}" if len(kalshi_code) <= 6 else f"TEMP{market_type_upper[:4]}{kalshi_code[-4:] if len(kalshi_code) > 4 else kalshi_code}"
    
    if strike:
        # Depending on how specific market URLs need to be formed
        return f"https://kalshi.com/markets/{series_ticker}/{strike}"
    else:
        return f"https://kalshi.com/markets/{series_ticker}"


def generate_legacy_market_url(station: str, market_type: str) -> str:
    """
    Legacy URL generation before adding strike parameter support
    """
    return generate_market_url(station, market_type)

# ─── Alert Schema Version ────────────────────────────────────────────────
PAPER_TRADE_ALERT_SCHEMA_VERSION = "2.1"  # B-MODE v2

# ─── Cooldown Tracker ───────────────────────────────────────────────────
# Per-station, per-lane frequency throttle to prevent alert spam.


class AlertCooldown:
    """
    Per-station cooldown tracker for alert frequency throttling.

    Prevents the same station/lane from re-alerting before the cooldown
    period expires. Cooldown resets only on signal state transition
    (active→inactive or inactive→active), not on repeated confirms.

    Lane cooldown periods:
      - regular:     4 hours
      - sure_thing:  8 hours
      - spike_reversion (A3: renamed from goldilocks): 12 hours
    """

    def __init__(self):
        # station -> {lane -> {bucket -> {last_alert_time, signal_state}}}
        # bucket is the trading bucket (temperature target), None if unknown
        self._cooldowns: Dict[str, Dict[str, Dict[str, Dict[str, Any]]]] = {}
        self._cooldown_periods = {
            'regular': 4 * 3600,
            'sure_thing': 8 * 3600,
            'spike_reversion': 12 * 3600,  # A3: renamed from goldilocks
            'goldilocks': 12 * 3600,  # backward compat
        }
        self._logger = logging.getLogger(__name__)

    def _key(self, station: str, lane: str, bucket: Optional[Any] = None) -> str:
        """Build a composite cooldown key from station, lane, and bucket.

        Bucket can be:
        - int: temperature bucket (e.g., 70, 72 for 70°F, 72°F)
        - str: Kalshi market ticker (e.g., "KXHIGHDEN-70")
        - None: no bucket info available
        """
        lane = lane.lower()
        if lane in ('regular', 'sure_thing', 'spike_reversion', 'goldilocks'):
            lane_key = lane
        else:
            lane_map = {
                'regular': 'regular',
                'sure_thing': 'sure_thing',
                'spike_reversion': 'spike_reversion',
                'goldilocks': 'spike_reversion',  # A3: backward compat
            }
            lane_key = lane_map.get(lane, 'regular')

        bucket_str = str(bucket) if bucket is not None else "none"
        return f"{station}::{lane_key}::{bucket_str}"

    def _lane_key(self, lane: str) -> str:
        lane = lane.lower()
        if lane in ('regular', 'sure_thing', 'spike_reversion', 'goldilocks'):
            return lane
        lane_map = {
            'regular': 'regular',
            'sure_thing': 'sure_thing',
            'spike_reversion': 'spike_reversion',
            'goldilocks': 'spike_reversion',  # A3: backward compat
        }
        return lane_map.get(lane, 'regular')

    def can_alert(self, station: str, lane: str,
                  bucket: Optional[Any] = None,
                  current_signal_state: bool = True) -> Tuple[bool, str]:
        """
        Check if an alert can be fired for this station/lane/bucket.

        Each trading bucket gets its own independent cooldown, so changing
        from the 70°F bucket to the 72°F bucket is treated as a new
        opportunity — not blocked by the previous bucket's cooldown.

        Args:
            station: ICAO station code
            lane: Lane type string ('regular', 'sure_thing', 'goldilocks')
            bucket: Trading bucket (temperature target, e.g. 70, 72)
            current_signal_state: True if signal is active, False if inactive

        Returns:
            (can_alert: bool, reason: str)
        """
        now = time.time()
        lane_key = self._lane_key(lane)
        cooldown_sec = self._cooldown_periods.get(lane_key, 4 * 3600)
        composite_key = self._key(station, lane, bucket)

        # Initialize nested structure if needed
        if station not in self._cooldowns:
            self._cooldowns[station] = {}
        if lane_key not in self._cooldowns[station]:
            self._cooldowns[station][lane_key] = {}

        # Check if this exact bucket has a cooldown entry
        bucket_key = str(bucket) if bucket is not None else "none"
        if bucket_key not in self._cooldowns[station][lane_key]:
            self._cooldowns[station][lane_key][bucket_key] = {
                'last_alert_time': 0.0,
                'signal_state': current_signal_state,
            }
            self._logger.debug(
                "AlertCooldown: new entry for %s", composite_key
            )
            return True, "new_entry"

        entry = self._cooldowns[station][lane_key][bucket_key]
        last_alert = entry['last_alert_time']
        last_state = entry['signal_state']
        elapsed = now - last_alert

        # Check if signal state has transitioned
        state_changed = (last_state != current_signal_state)

        if state_changed:
            entry['signal_state'] = current_signal_state
            self._logger.debug(
                "AlertCooldown: state transition for %s, allowing alert",
                composite_key
            )
            return True, "state_transition_reset"

        # Check cooldown period
        if elapsed < cooldown_sec:
            remaining = cooldown_sec - elapsed
            self._logger.debug(
                "AlertCooldown: %s in cooldown (%.0fs remaining)",
                composite_key, remaining
            )
            return False, f"cooldown_active_{int(remaining)}s_remaining"

        return True, "cooldown_expired"

    def record_alert(self, station: str, lane: str,
                     bucket: Optional[Any] = None,
                     signal_state: bool = True) -> None:
        """
        Record that an alert was fired for this station/lane/bucket.

        Updates the last_alert_time to now, resetting the cooldown timer
        for this specific bucket only.

        Args:
            station: ICAO station code
            lane: Lane type string
            bucket: Trading bucket (temperature target, e.g. 70, 72)
            signal_state: Current signal state (active/inactive)
        """
        now = time.time()
        lane_key = self._lane_key(lane)

        if station not in self._cooldowns:
            self._cooldowns[station] = {}
        if lane_key not in self._cooldowns[station]:
            self._cooldowns[station][lane_key] = {}

        bucket_key = str(bucket) if bucket is not None else "none"
        self._cooldowns[station][lane_key][bucket_key] = {
            'last_alert_time': now,
            'signal_state': signal_state,
        }

        self._logger.debug(
            "AlertCooldown: recorded alert for %s",
            self._key(station, lane, bucket)
        )

    def get_cooldown_status(self, station: str,
                            lane: str,
                            bucket: Optional[Any] = None) -> Dict[str, Any]:
        """Get current cooldown status for a station/lane/bucket."""
        lane_key = self._lane_key(lane)
        cooldown_sec = self._cooldown_periods.get(lane_key, 4 * 3600)

        if station not in self._cooldowns:
            return {
                'station': station,
                'lane': lane_key,
                'bucket': bucket,
                'in_cooldown': False,
                'remaining_sec': 0,
                'cooldown_period_sec': cooldown_sec,
                'last_alert_time': None,
                'signal_state': None,
            }

        lane_entry = self._cooldowns[station].get(lane_key)
        if lane_entry is None:
            return {
                'station': station,
                'lane': lane_key,
                'bucket': bucket,
                'in_cooldown': False,
                'remaining_sec': 0,
                'cooldown_period_sec': cooldown_sec,
                'last_alert_time': None,
                'signal_state': None,
            }

        bucket_key = str(bucket) if bucket is not None else "none"
        entry = lane_entry.get(bucket_key)
        if entry is None:
            return {
                'station': station,
                'lane': lane_key,
                'bucket': bucket,
                'in_cooldown': False,
                'remaining_sec': 0,
                'cooldown_period_sec': cooldown_sec,
                'last_alert_time': None,
                'signal_state': None,
            }

        now = time.time()
        elapsed = now - entry['last_alert_time']
        remaining = max(0, cooldown_sec - elapsed)

        return {
            'station': station,
            'lane': lane_key,
            'bucket': bucket,
            'in_cooldown': remaining > 0,
            'remaining_sec': int(remaining),
            'cooldown_period_sec': cooldown_sec,
            'last_alert_time': entry['last_alert_time'],
            'signal_state': entry['signal_state'],
        }

    def reset_station(self, station: str) -> None:
        """Clear all cooldown state for a station."""
        self._cooldowns.pop(station, None)

    def reset_all(self) -> None:
        """Clear all cooldown state."""
        self._cooldowns.clear()


# Singleton instance for module-level access
_ALERT_COOLDOWN = AlertCooldown()


def get_alert_cooldown() -> AlertCooldown:
    """Get the module-level AlertCooldown singleton."""
    return _ALERT_COOLDOWN


# ─── Lane Classification ────────────────────────────────────────────────
# Lane variants for alert routing and formatting


class LaneType(Enum):
    REGULAR = "regular"           # Standard signals (confidence 50-70%)
    SURE_THING = "sure_thing"     # High confidence (≥70%)
    SPIKE_REVERSION = "spike_reversion"  # Tier 1 protected signals (A3: renamed from goldilocks)
    GOLDILOCKS = "goldilocks"      # A3: backward compat alias — prefer SPIKE_REVERSION


# Lane-specific configurations
LANE_CONFIG = {
    LaneType.REGULAR: {
        "label": "Regular",
        "min_confidence": 0.50,
        "max_confidence": 0.70,
        "position_multiplier": 1.0,
        "alert_format": "embed",
    },
    LaneType.SURE_THING: {
        "label": "Sure Thing",
        "min_confidence": 0.70,
        "max_confidence": 1.0,
        "position_multiplier": 1.5,
        "alert_format": "embed",
    },
    LaneType.SPIKE_REVERSION: {
        "label": "Spike Reversion",
        "min_confidence": 0.40,  # Can be lower for protected signals
        "max_confidence": 0.85,
        "position_multiplier": 1.2,
        "alert_format": "embed",
    },
    LaneType.GOLDILOCKS: {  # A3: backward compat
        "label": "Spike Reversion (legacy)",
        "min_confidence": 0.40,  # Can be lower for protected signals
        "max_confidence": 0.85,
        "position_multiplier": 1.2,
        "alert_format": "embed",
    },
}

# ─── Opportunity Grade Scale ────────────────────────────────────────────
# S/A/B/C/D/F based on composite metrics


class OpportunityGrade(Enum):
    S = "S"  # Exceptional (Edge ≥ 12%, Sharpe ≥ 2.0)
    A = "A"  # Excellent (Edge ≥ 9%, Sharpe ≥ 1.8)
    B = "B"  # Good (Edge ≥ 6%, Sharpe ≥ 1.5)
    C = "C"  # Average (Edge ≥ 3%, Sharpe ≥ 1.2)
    D = "D"  # Marginal (Edge ≥ 1%, Sharpe ≥ 1.0)
    F = "F"  # Weak (Edge < 1%, Sharpe < 1.0)


# Grade thresholds
GRADE_THRESHOLDS = {
    OpportunityGrade.S: {"edge_min": 0.12, "sharpe_min": 2.0},
    OpportunityGrade.A: {"edge_min": 0.09, "sharpe_min": 1.8},
    OpportunityGrade.B: {"edge_min": 0.06, "sharpe_min": 1.5},
    OpportunityGrade.C: {"edge_min": 0.03, "sharpe_min": 1.2},
    OpportunityGrade.D: {"edge_min": 0.01, "sharpe_min": 1.0},
    OpportunityGrade.F: {"edge_min": 0.00, "sharpe_min": 0.0},
}

# ─── Hard Filter Thresholds ─────────────────────────────────────────────
# Alerts are NOT emitted if ANY of these conditions are met


class AlertFilterResult(Enum):
    PASS = "pass"       # Alert can be emitted
    SKIP = "skip"       # Skip this alert due to filtering


def check_alert_filter(trade_confidence: float, market_prob: float,
                       Sharpe: float, signal_type: str) -> Tuple[AlertFilterResult, str]:
    """
    Apply hard filters to determine if alert should be emitted.
    
    DO NOT emit alert if:
    - Edge < +10 percentage points
    - Trade Conf < 65%
    - Opportunity Grade is D or F
    - Edge is negative
    
    Returns: (result, reason)
    """
    edge = trade_confidence - market_prob
    
    # Filter 1: Edge is negative
    if edge < 0:
        return AlertFilterResult.SKIP, f"Negative edge ({edge:.2%})"
    
    # Filter 2: Edge < +10 percentage points (use >= 0.10 for filter pass)
    if edge < 0.10:
        return AlertFilterResult.SKIP, f"Edge too low ({edge:.2%} < 10%)"
    
    # Filter 3: Trade Conf < 50% (aligned with REGULAR lane minimum)
    # BUG FIX: Was 65% which excluded Regular lane signals (50-70% range)
    if trade_confidence < 0.50:
        return AlertFilterResult.SKIP, f"Conf too low ({trade_confidence:.0%} < 50%)"
    
    # Filter 4: Opportunity Grade is D or F
    grade, _ = compute_opportunity_grade(trade_confidence, market_prob, Sharpe)
    if grade in (OpportunityGrade.D, OpportunityGrade.F):
        return AlertFilterResult.SKIP, f"Grade {grade.value} (D/F rejected)"
    
    return AlertFilterResult.PASS, "All filters passed"


# ─── Alert Builder/Renderer ─────────────────────────────────────────────


def compute_opportunity_grade(trade_confidence: float, market_prob: float,
                              Sharpe: float) -> Tuple[OpportunityGrade, float]:
    """
    Compute Opportunity Grade (S/A/B/C/D/F) based on Trade Conf, Market prob, and Sharpe.

    Edge = Trade Conf - Market prob
    Grade determined by Edge and Sharpe ratio thresholds.

    Returns: (grade, edge_value)
    """
    edge = trade_confidence - market_prob

    # Find the highest grade that meets thresholds
    for grade in [OpportunityGrade.S, OpportunityGrade.A, OpportunityGrade.B,
                  OpportunityGrade.C, OpportunityGrade.D, OpportunityGrade.F]:
        thresholds = GRADE_THRESHOLDS[grade]
        if edge >= thresholds["edge_min"] and Sharpe >= thresholds["sharpe_min"]:
            return grade, edge

    return OpportunityGrade.F, edge


def classify_lane(trade_confidence: float, signal_type: str) -> LaneType:
    """
    Classify signal into lane (Regular, Sure_Thing, Spike_Reversion).

    Spike_Reversion (A3: formerly Goldilocks): Tier 1 protected signals (bypass market eligibility)
    Sure_Thing: High confidence (≥70%)
    Regular: Standard confidence (50-70%)
    """
    # Check for Spike Reversion protected signals (A3: renamed from goldilocks)
    spike_reversion_signals = {
        "spike_reversion",
        "microstructure_spike_reversion",
        "microstructure_spike_momentum_down",
        "goldilocks_reversion",       # backward compat
        "goldilocks_reversion_alert",  # backward compat
        "goldilocks_momentum_down",    # backward compat
        "reversion_after_settlement",
    }

    if signal_type in spike_reversion_signals:
        return LaneType.SPIKE_REVERSION

    if trade_confidence >= 0.70:
        return LaneType.SURE_THING

    return LaneType.REGULAR


# ─── Discord Color Mapping by Grade ─────────────────────────────────────

GRADE_COLORS = {
    "S": 0xFFD700,   # Gold
    "A": 0xC0C0C0,   # Silver
    "B": 0xCD7F32,   # Bronze
    "C": 0x1E90FF,   # Dodger Blue
    "D": 0xFF8C00,   # Dark Orange
    "F": 0xFF0000,   # Red
}


def build_paper_trade_alert(trade_result: Dict[str, Any], station: str,
                           market_type: str, direction: str,
                           current_bucket: int = None,
                           trading_bucket: int = None,
                           instance: str = None, hit_rate: float = None,
                           hit_rate_n: int = None) -> Dict[str, Any]:
    """
    Build a slim paper-trade alert with S/A/B/C/D/F Opportunity Grade + Edge.
    Uses Discord embed format for distinct, readable blocks.
    
    Args:
        trade_result: Trade execution result with analytical_prob, market_price, confidence
        station: Station ICAO code
        market_type: HIGH or LOW
        direction: UP or DOWN
        current_bucket: Current bucket (optional, for bucket tracking)
        trading_bucket: Trading bucket (optional, for bucket tracking)
        instance: Instance tag (PROD/DEV/SBOX)
        hit_rate: Directional accuracy for this station+direction (0.0-1.0)
        hit_rate_n: Number of samples for hit rate (historical count)

    Returns:
        Alert payload dict with Discord embed structure, grade, edge, lane info
    """
    if instance is None:
        instance = INSTANCE

    # Extract values from trade result
    trade_confidence = trade_result.get("confidence", 0.5)
    market_prob = trade_result.get("market_price", 0.5)
    analytical_prob = trade_result.get("analytical_prob", 0.5)
    position_size = trade_result.get("position_size_usd", 0)
    Sharpe = trade_result.get("sharpe", 1.0)
    signal_type = trade_result.get("functionality", "unknown")
    trade_uuid = trade_result.get("trade_uuid", "N/A")

    # Build Kalshi market URL (real URL)
    market_url = generate_market_url(station, market_type, str(trading_bucket) if trading_bucket else None)

    # Compute Opportunity Grade and Edge
    grade, edge = compute_opportunity_grade(trade_confidence, market_prob, Sharpe)

    # Classify lane
    lane = classify_lane(trade_confidence, signal_type)
    lane_config = LANE_CONFIG[lane]

    # Determine the bucket discriminator for cooldown tracking.
    # Priority: market_ticker (most specific) > trading_bucket > analytical_prob %
    # The market_ticker includes the strike price (e.g. "KXHIGHDEN-70"), so
    # different strikes get independent cooldowns — same station/lane but
    # different target = new opportunity.
    bucket_key = (
        trade_result.get('market_ticker')
        or trading_bucket
        or int(analytical_prob * 100)
    )

    # Check frequency throttle cooldown before hard filters
    cooldown = get_alert_cooldown()
    can_alert, cooldown_reason = cooldown.can_alert(station, lane.value, bucket=bucket_key)
    if not can_alert:
        return {
            "content": None,
            "skip_reason": f"Cooldown: {cooldown_reason}",
            "filtered": True,
            "cooldown_skip": True,
            "cooldown_reason": cooldown_reason,
        }

    # Apply hard filters - skip alert if any filter fails
    filter_result, filter_reason = check_alert_filter(
        trade_confidence, market_prob, Sharpe, signal_type
    )
    
    if filter_result == AlertFilterResult.SKIP:
        return {
            "content": None,  # Signal alert should be skipped
            "skip_reason": filter_reason,
            "filtered": True,
        }

    # Record the alert in the cooldown tracker (only after all checks pass)
    cooldown.record_alert(station, lane.value, bucket=bucket_key)

    # Build Discord embed
    edge_pct = f"{edge:+.2%}"
    confidence_level = "HIGH" if trade_confidence >= 0.70 else "MEDIUM" if trade_confidence >= 0.50 else "LOW"
    sharpe_display = f"{Sharpe:.1f}"
    
    # Bucket tracking (if provided)
    bucket_line = ""
    if current_bucket is not None and trading_bucket is not None:
        bucket_line = f"Bucket: {current_bucket}→{trading_bucket}"
    
    # Hit rate line (if available)
    hit_rate_line = ""
    if hit_rate is not None and hit_rate_n is not None:
        hit_rate_pct = f"{hit_rate * 100:.0f}%"
        hit_rate_line = f"Hit rate: {hit_rate_pct} (n={hit_rate_n})"

    # Build embed fields
    fields = [
        {"name": "Station", "value": station, "inline": True},
        {"name": "Market", "value": market_type, "inline": True},
        {"name": "Direction", "value": direction, "inline": True},
        {"name": "Size", "value": f"${position_size:.2f}", "inline": True},
        {"name": "Confidence", "value": f"{confidence_level} ({trade_confidence:.0%})", "inline": True},
        {"name": "Market Prob", "value": f"{market_prob:.2%}", "inline": True},
        {"name": "Edge", "value": edge_pct, "inline": True},
        {"name": "Sharpe", "value": sharpe_display, "inline": True},
    ]
    
    if bucket_line:
        fields.append({"name": "Trading Bucket", "value": bucket_line, "inline": True})
    
    if hit_rate_line:
        fields.append({"name": "Performance", "value": hit_rate_line, "inline": False})
    
    # Build embed structure
    embed = {
        "title": f"[{instance}] Weather Trade Alert",
        "description": f"**Opportunity Grade: {grade.value}** • Lane: {lane_config['label']}",
        "color": GRADE_COLORS.get(grade.value, 0x808080),
        "fields": fields,
        "url": market_url,
        "footer": {
            "text": f"Trade UUID: {trade_uuid} | v2.1 | {signal_type}"
        }
    }
    
    return {
        "content": None,
        "embeds": [embed],
        "schema_version": PAPER_TRADE_ALERT_SCHEMA_VERSION,
        "grade": grade.value,
        "grade_label": grade.name,
        "edge": edge,
        "edge_pct": edge_pct,
        "lane": lane.value,
        "lane_label": lane_config['label'],
        "trade_confidence": trade_confidence,
        "market_prob": market_prob,
        "Sharpe": Sharpe,
        "hit_rate": hit_rate,
        "hit_rate_n": hit_rate_n,
        "market_url": market_url,
        "instance": instance,
        "station": station,
        "market_type": market_type,
        "direction": direction,
        "filtered": False,
    }


def format_alert_for_discord(alert_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Format alert data for Discord webhook delivery.
    
    Returns: Payload dict with content, embeds, and metadata
    """
    payload = {
        "username": f"Weather Engine [{alert_data.get('instance', 'DEV')}]",
    }
    
    if alert_data.get("content"):
        payload["content"] = alert_data["content"]
    
    if alert_data.get("embeds"):
        payload["embeds"] = alert_data["embeds"]
    
    return payload


def build_paper_trade_alert_dev(trade_result: Dict[str, Any], station: str,
                                market_type: str, direction: str,
                                current_bucket: int = None,
                                trading_bucket: int = None,
                                instance: str = None, hit_rate: float = None,
                                hit_rate_n: int = None) -> Dict[str, Any]:
    """
    DEV variant: Build paper-trade alert with Enhanced Opportunity Grade.
    
    Uses same filtering logic as main builder but DEV formatting.
    
    Args:
        trade_result: Trade execution result with analytical_prob, market_price, confidence
        station: Station ICAO code
        market_type: HIGH or LOW
        direction: UP or DOWN
        current_bucket: Current bucket (optional, for bucket tracking)
        trading_bucket: Trading bucket (optional, for bucket tracking)
        instance: Instance tag (PROD/DEV/SBOX)
        hit_rate: Directional accuracy for this station+direction (0.0-1.0)
        hit_rate_n: Number of samples for hit rate (historical count)

    Returns:
        Alert payload dict with Discord embed structure, grade, edge, lane info
    """
    return build_paper_trade_alert(
        trade_result=trade_result,
        station=station,
        market_type=market_type,
        direction=direction,
        current_bucket=current_bucket,
        trading_bucket=trading_bucket,
        instance=instance,
        hit_rate=hit_rate,
        hit_rate_n=hit_rate_n,
    )
