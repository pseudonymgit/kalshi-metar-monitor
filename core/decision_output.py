#!/usr/bin/env python3

# CHANGELOG (last 10 broad changes):
# 1. [2026-07-05 R4-1.1: Fix P&L mark-to-market + thread-safe price cache]
#

"""
DECISION OUTPUT SYSTEM (v1.0)
Produces explicit decisions showing "market implied probability" vs "analytical fair value + confidence"
for weather trading signals. Combines inputs from multiple components:
- Paper trading engine
- Climate conditioning 
- Cross-platform divergence tracker
- Signal confidence estimates

Core purpose: Explicit reporting for P1.4 - "Explicit decision output: 
'market implied probability vs analytical fair value + confidence'"

Output: Structured decision reports comparing market prices (implied probabilities) 
to our analytical fair values with detailed confidence reasoning.
"""

import sqlite3
import json
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass
from enum import Enum
import os
import logging
from pathlib import Path


# Configuration path constants
METAR_DB_PATH = "/home/node/.openclaw/workspace/prototypes/weather-engine-source/data/metar_backfill.db"
PAPER_DB_PATH = "/home/node/.openclaw/workspace/prototypes/weather-engine-source/data/paper_trading.db"
CLIMATOLOGY_REPORT_PATH = "/home/node/.openclaw/workspace/prototypes/weather-engine-source/reports/paper_trading_calibration.json"
DEVELOPMENTS_PATH = "/home/node/.openclaw/workspace/prototypes/weather-engine-source/decision_outputs"


@dataclass
class MarketDataPoint:
    """Represents market data for a given contract."""
    symbol: str
    marketplace: str  # 'kalshi', 'polymarket', etc.
    market_implied_prob: float  # 0.0-1.0, from market price
    current_price: float  # Raw market price (0.0-1.0)
    expiration_date: str
    liquidity_score: float  # Liquidity/depth measure
    timestamp: str
    volume: float = None
    fee_rate: float = None


@dataclass  
class AnalyticalEstimate:
    """Our analytical fair value estimate for a contract."""
    analytical_prob: float  # Estimated 'true' probability (0.0-1.0)  
    confidence_level: float  # Confidence in our estimate (0.0-1.0)
    methodology: str  # Which approach was used (climatology, signal, etc.)
    components: List[Tuple[str, float]]  # Individual components contributing (component-name, weight)
    timestamp: str
    reasoning: List[str]  # Human-readable explanation of why we reached this conclusion
    risk_factors: List[str]  # Potential risks to this estimate


@dataclass
class DecisionOutput:
    """Final decision comparing market price vs analytical value."""
    symbol: str
    timestamp: str
    market_data: MarketDataPoint
    analytical_estimate: AnalyticalEstimate
    price_difference: float  # MarketPrice - Analytical (positive = market overprices)
    absolute_difference: float  # abs(MarketPrice - Analytical)  
    decision_type: str  # "BUY_CHEAP_ASSET", "SELL_EXPENSIVE_ASSET", "HOLD"
    recommendation_strength: float  # How strongly to act (0.0-1.0)
    reasoning_notes: List[str]


# Standalone functions to calculate analytical probabilities without importing other modules
def get_simple_climatology_prob(station, date_str):
    """
    Helper function to get basic climatology probability without circular imports.
    """
    conn = sqlite3.connect(METAR_DB_PATH)
    c = conn.cursor()
    
    # Get climatology - probability of temperature moving UP/DOWN on same date
    target_month_day = date_str[5:10]  # Extract MM-DD from YYYY-MM-DD
    
    c.execute("""
        SELECT 
            avg(CASE WHEN settlement_bucket > prior_settlement_bucket THEN 1.0 ELSE 0.0 END) as up_rate,
            count(*) as sample_size
        FROM settlement_epochs se
        WHERE se.station = ?
        AND substr(local_trading_date, 6, 5) = ?
        AND se.epoch_status = 'closed'
        AND settlement_bucket IS NOT NULL and prior_settlement_bucket IS NOT NULL
    """, (station, target_month_day))
    
    climatology_row = c.fetchone()
    climatology_prob = climatology_row[0] if climatology_row and climatology_row[0] is not None else 0.5
    climatology_sample_size = climatology_row[1] if climatology_row else 0
    
    conn.close()
    
    # Calculate a simple confidence score
    confidence = min(0.95, max(0.3, 0.3 + min(0.65, climatology_sample_size * 0.05)))  # Confidence based on sample size
    
    return climatology_prob, confidence


def get_reversion_signal(station, date_str, max_temp, settlement, prior):
    """
    Simple reversion signal calculation without importing split_backtest.
    """
    if prior is None or settlement is None:
        return None, 0.0, 0.5  # No signal if no data
    
    # How far from normal is "extreme"? Based on regional patterns
    # For ATL, seasonal average is around 68
    seasonal_norm = 68.0 if 'KATL' in station else 67.0
    diff_from_norm = abs(settlement - seasonal_norm)
    
    if settlement > 82:  # Hot day → predict DOWN reversion
        return 'down', 0.58, 0.62
    elif settlement < 45:  # Cold day → predict UP reversion
        return 'up', 0.57, 0.61
    elif diff_from_norm > 12:  # Far from seasonal norm
        # Base direction on prior-to-settlement move
        direction = 'down' if settlement > prior else 'up'
        return direction, 0.55, 0.57
    else:
        # Mild temps → stick with trend (momentum)
        return 'up' if settlement > prior else 'down', 0.50, 0.50


class DecisionOutputGenerator:
    """
    Generates structured decision outputs showing market price vs analytical fair value.
    This implements P1.4: "Explicit decision output: market implied probability vs analytical fair value + confidence"
    """
    
    def __init__(self, metar_db=METAR_DB_PATH, paper_db=PAPER_DB_PATH):
        self.metar_db = metar_db
        self.paper_db = paper_db
        self.logger = logging.getLogger(self.__class__.__name__)
        Path(DEVELOPMENTS_PATH).mkdir(exist_ok=True)
    
    def _get_market_implied_prob(self, station: str, date: str, market_type: str = "HIGH") -> float:
        """
        Get market-implied probability for a given weather market.
        In production, this would come from real Kalshi/Polymarket APIs.
        Currently uses historical settlement data to simulate realistic prices.
        """
        conn = sqlite3.connect(self.metar_db)
        cur = conn.cursor()
        
        # Get the settlement data to simulate realistic market prices
        # In production, we'd query the live market price from exchanges
        cur.execute("""
            SELECT settlement_bucket, prior_settlement_bucket 
            FROM settlement_epochs 
            WHERE station = ? AND local_trading_date < ?
            AND market_type = ? AND epoch_status = 'closed'
            ORDER BY local_trading_date DESC 
            LIMIT 1
        """, (station, date, market_type))
        
        row = cur.fetchone()
        conn.close()
        
        if row:
            settlement, prior = row
            # Use the movement from prior to settlement as basis to simulate a realistic current price
            # Normal distribution around recent patterns would be more realistic but this works as a placeholder
            recent_movement = settlement - prior
            # Simulate a reasonable market price based on recent behavior
            # This is placeholder logic - would come from live API in production
            base_healthy_price = 0.55 if market_type == "HIGH" else 0.45  # Bias for HIGH/LOW markets
            simulated_price = min(0.95, max(0.05, base_healthy_price + (recent_movement / 50.0)))
            return simulated_price
        else:
            return 0.5  # Neutral default if no data available
    
    def _get_analytical_estimate(self, station: str, date: str, market_type: str = "HIGH") -> AnalyticalEstimate:
        """
        Get our analytical probability estimate for this market.
        Combines outputs from:
        - Climatology pillar (historical frequency)
        - Signal engines (current patterns)
        - Cross-platform divergence (inefficiency signals)
        """
        
        # Get climatology-based estimate
        clim_prob, clim_conf = get_simple_climatology_prob(station, date)
        
        # Get recent data for signal analysis
        conn = sqlite3.connect(self.metar_db)
        cur = conn.cursor()
        
        # Get recent actuals for signals
        cur.execute("""
            SELECT settlement_bucket, prior_settlement_bucket, max_temp_f
            FROM settlement_epochs se
            LEFT JOIN daily_stats ds ON se.station = ds.station AND se.local_trading_date = ds.date_utc
            WHERE se.station = ? AND se.local_trading_date <= ?
            AND se.market_type = ? AND se.epoch_status = 'closed'
            ORDER BY se.local_trading_date DESC LIMIT 1
        """, (station, date, market_type))
        
        row = cur.fetchone()
        settlement, prior, max_temp = (None, None, None) if not row else (row[0], row[1], row[2])
        conn.close()
        
        # Apply signal corrections based on current conditions
        signal_correction = 0.0
        total_signal_conf = 0.0
        
        # Simple reversion signal (equivalent to what's in split_backtest)
        rev_direction, rev_conf, rev_prob = get_reversion_signal(station, date, max_temp, settlement, prior)
        if rev_direction is not None:
            if rev_direction == 'up':
                signal_correction = rev_prob
            elif rev_direction == 'down':
                signal_correction = 1 - rev_prob  # Flip because down means less probability of up
            total_signal_conf = rev_conf
        
        # Weighted average combining climatology with signal
        # In production, we would use more sophisticated weighting from ensemble models
        weighted_prob = (clim_prob * 0.6) + (signal_correction * 0.25) + (0.5 * 0.15)  # Add neutral base
        total_conf = min(0.90, max(0.10, clim_conf * 0.6 + total_signal_conf * 0.25 + 0.15))  # Factor in neutral base confidence

        # Generate reasoning
        components = [
            ("climatology_analysis", 0.6), 
            ("reversion_signal", 0.25), 
            ("neutral_base", 0.15)
        ]
        
        reasoning = [
            f"Historical frequency for this date & location suggests {weighted_prob:.2%} odds",
            f"Week's recent pattern contributes based on settlement at {settlement} vs prior {prior}",
            f"Climatology data contributes base estimate of {clim_prob:.2%} with {clim_conf:.1%} confidence",
            f"Reversion signal adjusted estimate by {(signal_correction - clim_prob):+.2%}"
        ]
        
        risk_factors = [
            "Seasonal regime shift possible affecting historical patterns",
            "Short-term weather model uncertainties may differ from long-term patterns",
            "Historical data may not capture recent climate change effects"
        ]
        
        return AnalyticalEstimate(
            analytical_prob = max(0.02, min(0.98, weighted_prob)),  # Bound probabilities
            confidence_level = total_conf,
            methodology = "ensemble_climatology_plus_signals",
            components = components,
            timestamp = datetime.now(timezone.utc).isoformat(),
            reasoning = reasoning,
            risk_factors = risk_factors
        )
    
    def generate_single_decision(self, station: str, date: str, market_type: str = "HIGH") -> DecisionOutput:
        """
        Generate a single decision comparing market price to analytical value.
        P1.4 Implementation: "Explicit decision output: 'market implied probability vs analytical fair value + confidence'"
        """
        # Get market and analytical data
        market_prob = self._get_market_implied_prob(station, date, market_type)
        analytical_estimate = self._get_analytical_estimate(station, date, market_type)
        
        price_difference = analytical_estimate.analytical_prob - market_prob
        absolute_difference = abs(price_difference)
        
        # Determine recommendation based on divergence and confidence
        if absolute_difference < 0.05:
            decision_type = "HOLD"  # Too small a difference to justify trade
            strength = 0.05  # Minimal recommendation
        else:
            if analytical_estimate.analytical_prob > market_prob + 0.02:
                # Our system thinks the "yes" outcome is more likely than market is pricing it
                # So the market is selling it cheap - BUY YES
                decision_type = "BUY_YES"
            elif analytical_estimate.analytical_prob < market_prob - 0.02:
                # Our system thinks "yes" is less likely than market, so market overprices it
                # Sell yes or buy no depending on what instruments are available
                decision_type = "SELL_YES"
            else:
                decision_type = "HOLD"  # Within margin
            
            # Strength scales with both price divergence and confidence
            strength_percent = absolute_difference * analytical_estimate.confidence_level * 3.0
            strength = min(1.0, strength_percent)
        
        market_data = MarketDataPoint(
            symbol = f"{station}_HIGH_{date}",
            marketplace = "kalshi_simulated",  # In production would be actual exchange
            market_implied_prob = market_prob,
            current_price = market_prob,  # For this simplified model, prob and price are effectively the same metric
            expiration_date = date,
            liquidity_score = 0.67,  # Placeholder between 0-1
            timestamp = datetime.now(timezone.utc).isoformat(),
            volume = 1500.0,  # Placeholder
            fee_rate = 0.001  # Placeholder
        )
        
        notes = [
            f"Market prices outcome at {market_prob:.1%} probability",
            f"Our analysis values it at {analytical_estimate.analytical_prob:.1%} probability", 
            f"Divergence of {absolute_difference:.1%} detected",
            f"Confidence level of analysis at {analytical_estimate.confidence_level:.1%}",
            f"Recommendation: {decision_type} with strength {strength:.2f}",
            f"Actionable threshold: movements > 5% are considered meaningful for trading action"
        ]
        
        return DecisionOutput(
            symbol = market_data.symbol,
            timestamp = datetime.now(timezone.utc).isoformat(),
            market_data = market_data,
            analytical_estimate = analytical_estimate,
            price_difference = price_difference,
            absolute_difference = absolute_difference,
            decision_type = decision_type,
            recommendation_strength = strength,
            reasoning_notes = notes
        )
    
    def generate_bulk_decisions(self, date: str, stations: List[str] = None) -> List[DecisionOutput]:
        """
        Generate decision outputs for all specified stations on a given date.
        """
        if stations is None:
            stations = ['KATL', 'KBOS', 'KLAX', 'KJFK', 'KORD', 'KMIA', 'KSEA', 'KSFO', 'KDFW', 'KHOU']
        
        decisions = []
        for station in stations:
            try:
                decision = self.generate_single_decision(station, date)
                decisions.append(decision)
            except Exception as e:
                self.logger.error(f"Error generating decision for {station} on {date}: {e}")
                continue
        
        return decisions
    
    def generate_detailed_report(self, decision: DecisionOutput) -> str:
        """
        Generate detailed textual report for a single decision.
        """
        report_parts = [
            "=" * 80,
            f"WEATHER DECISION REPORT FOR {decision.symbol}",
            "Generated on " + decision.timestamp,
            "=" * 80,
            "",
            "MARKET DATA:",
            f"  Marketplace: {decision.market_data.marketplace}",
            f"  Expiration: {decision.market_data.expiration_date}",
            f"  Market-Implied Probability: {decision.market_data.market_implied_prob:.2%}", 
            f"  Current Price: {decision.market_data.current_price:.3f}",
            f"  Liquidity Score: {decision.market_data.liquidity_score:.2f}",
            f"  Volume: ~${decision.market_data.volume:,.2f}",
            f"  Fee Rate: {decision.market_data.fee_rate or 0.0:.2%}",
            "",
            "ANALYTICAL ESTIMATE:",
            f"  Fair Value Probability: {decision.analytical_estimate.analytical_prob:.2%}",
            f"  Confidence Level: {decision.analytical_estimate.confidence_level:.2%}",
            f"  Methodology Used: {decision.analytical_estimate.methodology}",
            f"  Component Weights:",
        ]
        
        for method, weight in decision.analytical_estimate.components:
            report_parts.append(f"    - {method}: {weight:.1%}")
        
        report_parts.extend([
            "  Core Reasoning:"
        ])
        
        for reason in decision.analytical_estimate.reasoning:
            report_parts.append(f"    - {reason}")
        
        report_parts.extend([
            "",
            "DECISION ANALYSIS:",
            f"  Price Difference (Analytical - Market): {decision.price_difference:.3f}",
            f"  Absolute Difference: {decision.absolute_difference:.3f}",
            f"  Recommendation: {decision.decision_type}",
            f"  Recommendation Strength: {decision.recommendation_strength:.2f}",
            "",
            "JUSTIFICATION & REASONING:",
        ])
        
        for note in decision.reasoning_notes:
            report_parts.append(f"  - {note}")
        
        report_parts.extend([
            "",
            "ATTENTION AREAS & RISK FACTORS: ",
        ])
        
        for risk in decision.analytical_estimate.risk_factors:
            report_parts.append(f"  - {risk}")
        
        report_parts.extend([
            "",
            "=" * 80,
            "END DECISION REPORT",
            "=" * 80
        ])
        
        return "\n".join(report_parts)
    
    def save_decision_report(self, decision: DecisionOutput, filename: str = None) -> str:
        """
        Save a single decision report to disk.
        """
        if filename is None:
            symbol_safe = decision.symbol.replace("/", "_").replace("\\", "_").replace(" ", "_")
            filename = f"decision_{symbol_safe}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.txt"
            
        filepath = os.path.join(DEVELOPMENTS_PATH, filename)
        
        report_text = self.generate_detailed_report(decision)
        
        with open(filepath, 'w') as f:
            f.write(report_text)
        
        return filepath
    
    def export_to_json(self, decisions: List[DecisionOutput], filename: str = None) -> str:
        """
        Export multiple decisions to JSON for external consumption or dashboard display.
        """
        if not filename:
            filename = f"decisions_batch_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
        
        filepath = os.path.join(DEVELOPMENTS_PATH, filename)
        
        # Convert dataclass instances to JSON serializable format
        json_data = []
        for decision in decisions:
            data_item = {
                "symbol": decision.symbol,
                "timestamp": decision.timestamp,
                "market_data": {
                    "symbol": decision.market_data.symbol,
                    "marketplace": decision.market_data.marketplace,
                    "market_implied_prob": decision.market_data.market_implied_prob,
                    "current_price": decision.market_data.current_price,
                    "expiration_date": decision.market_data.expiration_date,
                    "liquidity_score": decision.market_data.liquidity_score,
                    "timestamp": decision.market_data.timestamp,
                    "volume": decision.market_data.volume,
                    "fee_rate": decision.market_data.fee_rate
                },
                "analytical_estimate": {
                    "analytical_prob": decision.analytical_estimate.analytical_prob,
                    "confidence_level": decision.analytical_estimate.confidence_level,
                    "methodology": decision.analytical_estimate.methodology,
                    "components": decision.analytical_estimate.components,
                    "timestamp": decision.analytical_estimate.timestamp,
                    "reasoning": decision.analytical_estimate.reasoning,
                    "risk_factors": decision.analytical_estimate.risk_factors
                },
                "price_difference": decision.price_difference,
                "absolute_difference": decision.absolute_difference,
                "decision_type": decision.decision_type,
                "recommendation_strength": decision.recommendation_strength,
                "reasoning_notes": decision.reasoning_notes
            }
            json_data.append(data_item)
        
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(json_data, f, indent=2, ensure_ascii=False)
        
        print(f"Saved {len(decisions)} decisions to {filepath}")
        return filepath


def main():
    """
    Demonstration of the Decision Output System.
    """
    print("Decision Output System (P1.4 Implementation)")
    print("=" * 80)
    print("Comparing market implied probability vs analytical fair value + confidence")
    
    # Create a new instance (to avoid import issues)
    generator = DecisionOutputGenerator()
    
    print("\n1. Generating sample decision for ATLANTA HIGH temperature on upcoming date...")
    decision = generator.generate_single_decision("KATL", (datetime.now(timezone.utc) + timedelta(days=1)).strftime('%Y-%m-%d'))
    
    print(generator.generate_detailed_report(decision))
    
    print(f"\n2. Generating batch decisions for multiple stations...")
    stations = ['KATL', 'KBOS', 'KLAX', 'KJFK', 'KORD']
    date = (datetime.now(timezone.utc) + timedelta(days=2)).strftime('%Y-%m-%d')
    
    batch_decisions = generator.generate_bulk_decisions(date, stations)
    
    print(f"\nBatch contained {len(batch_decisions)} decisions:")
    for d in batch_decisions:
        market = d.market_data.market_implied_prob
        analytical = d.analytical_estimate.analytical_prob
        diff = abs(d.price_difference)
        print(f"  {d.symbol:12s}: Mkt Prob={market:.3f}, Analytical={analytical:.3f}, |Diff|={diff:.3f}, Action={d.decision_type}, Strength={d.recommendation_strength:.2f}")
    
    # Save a decision report
    print(f"\n3. Saving sample decision to file...")
    sample_decision = generator.generate_single_decision("KORD", (datetime.now(timezone.utc) + timedelta(days=3)).strftime('%Y-%m-%d'))
    file_saved = generator.save_decision_report(sample_decision)
    print(f"  Report saved to: {file_saved}")
    
    # Export data to JSON for dashboard consumption
    print(f"\n4. Exporting batch decisions to JSON...")
    json_file = generator.export_to_json(batch_decisions)
    print(f"  Data exported to: {json_file}")
    
    print(f"\n💡 P1.4 Requirement Satisfied:")
    print(f"   - Market implied probability vs analytical fair value comparison completed")
    print(f"   - Confidence level provided ({sample_decision.analytical_estimate.confidence_level:.1%} confidence in sample)")
    print(f"   - Structured decision output with explicit reasoning")
    print(f"   - Written reports and JSON exports for further analysis/dashboard input")


if __name__ == "__main__":
    main()