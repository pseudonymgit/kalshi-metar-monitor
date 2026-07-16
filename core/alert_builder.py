#!/usr/bin/env python3
"""
Paper Trading Alert Builder/Renderer v2.1 (B-MODE v2)
Slim Discord embed layout with S/A/B/C/D/F Opportunity Grade + Edge calculation
Hard filtering applied before alert emission.
"""

from enum import Enum
from typing import Dict, Any, Tuple, Optional
import os

# Instance environment variable (PROD/DEV/SBOX)
INSTANCE = os.getenv("PAPER_TRADING_INSTANCE", "DEV").upper()

# ─── Alert Schema Version ────────────────────────────────────────────────
PAPER_TRADE_ALERT_SCHEMA_VERSION = "2.1"  # B-MODE v2

# ─── Lane Classification ────────────────────────────────────────────────
# Lane variants for alert routing and formatting


class LaneType(Enum):
    REGULAR = "regular"           # Standard signals (confidence 50-70%)
    SURE_THING = "sure_thing"     # High confidence (≥70%)
    GOLDILOCKS = "goldilocks"     # Tier 1 protected signals


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
    LaneType.GOLDILOCKS: {
        "label": "Goldilocks",
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
    
    # Filter 3: Trade Conf < 65%
    if trade_confidence < 0.65:
        return AlertFilterResult.SKIP, f"Conf too low ({trade_confidence:.0%} < 65%)"
    
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
    Classify signal into lane (Regular, Sure_Thing, Goldilocks).

    Goldilocks: Tier 1 protected signals (bypass market eligibility)
    Sure_Thing: High confidence (≥70%)
    Regular: Standard confidence (50-70%)
    """
    # Check for Goldilocks protected signals
    goldilocks_signals = {
        "goldilocks_reversion",
        "goldilocks_reversion_alert",
        "goldilocks_momentum_down",
        "reversion_after_settlement",
    }

    if signal_type in goldilocks_signals:
        return LaneType.GOLDILOCKS

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
    from kalshi_price_fetcher import build_market_url
    market_url = build_market_url(station, market_type)

    # Compute Opportunity Grade and Edge
    grade, edge = compute_opportunity_grade(trade_confidence, market_prob, Sharpe)

    # Classify lane
    lane = classify_lane(trade_confidence, signal_type)
    lane_config = LANE_CONFIG[lane]

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
