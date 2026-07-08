#!/usr/bin/env python3
"""
ALERT FORMATTER — New contract format for Kalshi trading alerts

Implements the new Dan-specified alert format:
- Timestamp (UTC)
- Real Kalshi market ticker
- One English sentence with all SH6 elements
- Real balance/position size 
- Hard gate for direction/market compatibility

Replaces the old placeholder format with structured, informational, actionable content.
"""

import json
from typing import Dict, Any, Optional, Tuple
from datetime import datetime, timezone
from urllib.parse import urlparse
from core.conviction import ConvictionScorer


class AlertFormatter:
    """
    Format alerts according to the new contract specification.
    
    Includes:
    - Real Kalshi market ticker
    - Timestamp-qualified one-sentence statement
    - All conviction/agreement elements
    - Hard gate validation
    """
    
    def __init__(self):
        self.conviction_scorer = ConvictionScorer()
    
    def format_new_alert(
        self,
        station: str,
        market_type: str,  # 'HIGH' or 'LOW'
        direction: str,    # 'UP' or 'DOWN' 
        event_ticker: str, # Full Kalshi market ticker 
        position_size: float,
        conviction_details: Dict[str, Any],  # From conviction module
        balance: float,
        fee_rate: float = 0.001,
        instance_tag: str = "[DEV]"
    ) -> Dict[str, Any]:
        """
        Format alert in the new contract format.
        
        Args:
            station: ICAO code
            market_type: 'HIGH' or 'LOW' 
            direction: 'UP' or 'DOWN'
            event_ticker: Full Kalshi market ticker
            position_size: Actual position size in dollars
            conviction_details: Output from conviction computation
            balance: Account balance
            fee_rate: Trading fee percentage
            instance_tag: Instance identifier
            
        Returns:
            Dict suitable for sending as Discord alert
        """
        timestamp_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        
        # Validate direction/market compatibility
        is_valid, validation_reason = self.conviction_scorer.assess_market_direction_validity(
            direction.lower(), 
            market_type.upper()
        )
        
        if not is_valid:
            return {
                "error": f"Alert rejected: {validation_reason}",
                "status": "invalid"
            }
        
        # Build the one-sentence English summary containing SH6 elements
        sentence = self._build_english_sentence(conviction_details)
        
        # Build complete message
        content_lines = [
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            f"{instance_tag} 💰 Time: {timestamp_utc} UTC",
            f"🏷️ Ticker: {event_ticker}",
            f"📊 Market: {market_type}",
            f"📈 Direction: {direction}",
            f"⚖️ Position: ${position_size:.2f}",
            f"🏦 Balance: ${balance:.2f}",
            f"📝 Sentence: {sentence}",
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        ]
        
        content = "\n".join(content_lines)
        
        return {
            "content": content,
            "username": f"Weather Engine {instance_tag}",
            "embeds": [],
            "formatted_at": timestamp_utc,
            "status": "valid"
        }
    
    def _build_english_sentence(self, conviction_details: Dict[str, Any]) -> str:
        """
        Build an English sentence containing all required elements from SH6.
        
        Format: "edge after fee N, agreement N, regime fit N, calibration state N"
        """
        edge_after_fee = conviction_details.get('edge_after_fee', 0.0)
        agreement_score = conviction_details.get('agreement_score', 0.0)
        regime_fit = conviction_details.get('regime_fit', 'unknown')
        calibration_state = conviction_details.get('calibration_state', {})
        ece = calibration_state.get('ece', 0.1)
        
        sentence = (f"Edge after fee {edge_after_fee:.3f}, "
                   f"agreement {agreement_score:.3f}, "
                   f"regime fit {regime_fit}, "
                   f"calibration state ECE {ece:.3f}")
        
        return sentence
    
    def format_detailed_alert(
        self,
        station: str,
        market_type: str,
        direction: str, 
        event_ticker: str,
        position_size: float,
        balance: float,
        conviction_details: Dict[str, Any],
        top_signals: Optional[list] = None,
        extra_metrics: Optional[Dict] = None,
        instance_tag: str = "[DEV]"
    ) -> Dict[str, Any]:
        """
        Detailed format including all metrics for development/debugging.
        """
        timestamp_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        
        # Validate direction/market compatibility 
        is_valid, validation_reason = self.conviction_scorer.assess_market_direction_validity(
            direction.lower(), 
            market_type.upper()
        )
        
        if not is_valid:
            return {
                "error": f"Detailed alert rejected: {validation_reason}",
                "status": "invalid"
            }
        
        content_lines = [
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            f"{instance_tag} 💰 Time: {timestamp_utc} UTC",
            f"📍 Station: {station}",
            f"🏷️ Ticker: {event_ticker}", 
            f"📊 Market: {market_type}",
            f"📈 Direction: {direction}",
            f"⚖️ Position: ${position_size:.2f}",
            f"🏦 Balance: ${balance:.2f}",
            f"💬 Sentence: {self._build_english_sentence(conviction_details)}",
        ]
        
        # Include conviction components breakdown
        comp = conviction_details.get('components', {})
        content_lines.append(f"🔍 Edge mag: {comp.get('edge_magnitude', 0):.3f}")
        content_lines.append(f"🤝 Agreement: {comp.get('agreement_factor', 0) - 0.3:.3f} (factor: {comp.get('agreement_factor', 0):.3f})")
        content_lines.append(f"🎯 Conviction: {conviction_details.get('conviction_score', 0):.3f}")
        
        # Calibration metrics
        cal = conviction_details.get('calibration_state', {})
        content_lines.append(f"📏 Cal State: ECE {cal.get('ece', 0):.3f}, Qual {cal.get('quality', 0):.3f}")
        
        # Regime fit
        content_lines.append(f"🔄 Regime Fit: {conviction_details.get('regime_fit', 'N/A')}")
        
        # Optional sections
        if top_signals:
            top = ', '.join(top_signals) if isinstance(top_signals, list) else str(top_signals)
            content_lines.append(f"🔝 Top Signals: {top}")
              
        if extra_metrics:
            for key, val in extra_metrics.items():
                content_lines.append(f"📊 {key.title()}: {val}")
        
        content_lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        
        content = '\n'.join(content_lines)
        
        return {
            "content": content,
            "username": f"Weather Engine {instance_tag}",
            "embeds": [],
            "formatted_at": timestamp_utc,
            "status": "valid"
        }


def format_alert(
    station: str,
    market_type: str,
    direction: str,
    event_ticker: str, 
    position_size: float,
    conviction_details: Dict[str, Any],
    balance: float,
    fee_rate: float = 0.001,
    instance_tag: str = "[DEV]"
) -> Dict[str, Any]:
    """
    Standalone function for backward compatibility.
    """
    formatter = AlertFormatter()
    return formatter.format_new_alert(
        station, market_type, direction, event_ticker, 
        position_size, conviction_details, balance, fee_rate, 
        instance_tag
    )


def format_detailed_alert(
    station: str,
    market_type: str,
    direction: str,
    event_ticker: str,
    position_size: float,
    balance: float,
    conviction_details: Dict[str, Any],
    top_signals=None,
    extra_metrics=None,
    instance_tag="[DEV]"
) -> Dict[str, Any]:
    """
    Standalone detailed format for development use.
    """
    formatter = AlertFormatter()
    return formatter.format_detailed_alert(
        station, market_type, direction, event_ticker,
        position_size, balance, conviction_details,
        top_signals, extra_metrics, instance_tag
    )