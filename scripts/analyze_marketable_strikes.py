#!/usr/bin/env python3
"""
H.6 — Marketable Strikes and Net Executable Edge Analyzer

Analyzes which temperature buckets actually trade on Kalshi markets,
measures net executable edge after fees/slippage, and provides
market-state classification for all 20 stations.

Focus: Real market liquidity and executable edge, not theoretical bucket accuracy.
"""

import argparse
import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Tuple, Optional

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="[H.6] %(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
LOGGER = logging.getLogger("h6_analyze_marketable_strikes")


class MarketableStrikeAnalyzer:
    """Analyze marketable strikes and net executable edge."""
    
    def __init__(self, data_dir: str = None):
        self.data_dir = data_dir or os.path.join(os.path.dirname(__file__), "..", "data")
        self.project_root = Path(self.data_dir).parent.absolute()
        
    def get_kalshi_snapshot_db_path(self) -> str:
        """Get path to kalshi market snapshots DB."""
        return os.path.join(self.data_dir, "kalshi_market_snapshots.db")
    
    def get_station_universe(self) -> List[str]:
        """Get the canonical 20-station universe."""
        # From the weather engine, the canonical 20 stations
        return [
            "KATL", "KAUS", "KBOS", "KDCA", "KDEN", 
            "KDFW", "KHOU", "KLAS", "KLAX", "KMDW", 
            "KMIA", "KMSP", "KMSY", "KNYC", "KOKC", 
            "KPHL", "KPHX", "KSAT", "KSEA", "KSFO"
        ]
    
    def analyze_current_liquidity(self, hours_back: int = 24) -> Dict:
        """Analyze current liquidity across all stations."""
        db_path = self.get_kalshi_snapshot_db_path()
        if not os.path.exists(db_path):
            LOGGER.warning(f"Kalshi snapshots DB not found at {db_path}")
            return {"error": f"Database not found: {db_path}"}
        
        conn = sqlite3.connect(db_path)
        try:
            # Get snapshots from the last N hours
            cutoff_time = datetime.utcnow() - timedelta(hours=hours_back)
            cutoff_iso = cutoff_time.strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")
            
            # Query for recent market snapshots
            query = """
                SELECT 
                    station,
                    market_ticker,
                    strike_temp,
                    market_type,
                    yes_bid,
                    yes_ask,
                    mid_price,
                    volume_24h,
                    open_interest
                FROM market_snapshots 
                WHERE snapshot_time > ?
                ORDER BY snapshot_time DESC
            """
            
            cursor = conn.execute(query, (cutoff_iso,))
            rows = cursor.fetchall()
            
            # Organize data by station
            station_data = {}
            for row in rows:
                station, ticker, strike, mtype, bid, ask, mid, vol, oi = row
                
                if station not in station_data:
                    station_data[station] = {
                        "markets": [],
                        "active_count": 0,
                        "volume_sum": 0,
                        "oi_sum": 0
                    }
                
                if bid is not None and ask is not None:
                    spread = ask - bid
                    station_data[station]["markets"].append({
                        "ticker": ticker,
                        "strike": strike,
                        "type": mtype,
                        "bid": bid,
                        "ask": ask,
                        "mid": mid,
                        "spread_bps": spread * 10000 if spread is not None else None,
                        "volume_24h": vol,
                        "open_interest": oi
                    })
                    
                    # Count as active if bid/ask exist
                    station_data[station]["active_count"] += 1
                    if vol:
                        station_data[station]["volume_sum"] += vol
                    if oi:
                        station_data[station]["oi_sum"] += oi
            
            # Calculate liquidity metrics per station
            results = {}
            for station in self.get_station_universe():
                if station in station_data:
                    data = station_data[station]
                    results[station] = {
                        "status": "active" if data["active_count"] > 0 else "inactive",
                        "market_count": data["active_count"],
                        "total_volume_24h": data["volume_sum"],
                        "total_open_interest": data["oi_sum"],
                        "markets": data["markets"][:5]  # Top 5 markets per station
                    }
                else:
                    results[station] = {
                        "status": "no_data",
                        "market_count": 0,
                        "total_volume_24h": 0,
                        "total_open_interest": 0,
                        "markets": []
                    }
            
            # Overall summary
            active_stations = sum(1 for v in results.values() if v["status"] == "active")
            total_markets = sum(v["market_count"] for v in results.values())
            
            summary = {
                "analysis_period_hours": hours_back,
                "total_stations": len(self.get_station_universe()),
                "active_stations": active_stations,
                "inactive_stations": len(self.get_station_universe()) - active_stations,
                "total_markets": total_markets,
                "station_breakdown": results
            }
            
            return summary
            
        finally:
            conn.close()
    
    def calculate_executable_edge(self, bid: float, ask: float, predicted_prob: float) -> Dict:
        """Calculate net executable edge after considering bid-ask spread."""
        if bid is None or ask is None:
            return {"executable": False, "edge_bps": 0, "reason": "no_quotes"}
        
        # Mid price
        mid = (bid + ask) / 2.0
        spread = ask - bid
        
        # Calculate edge in basis points
        if predicted_prob > mid:
            # Predicted up, would buy at ask
            edge_bps = (predicted_prob - ask) * 10000
            execution_cost_bps = spread * 10000 / 2  # Half spread cost
            net_edge_bps = edge_bps - execution_cost_bps
            executable = net_edge_bps > 0
        elif predicted_prob < bid:
            # Predicted down, would sell at bid
            edge_bps = (bid - predicted_prob) * 10000
            execution_cost_bps = spread * 10000 / 2  # Half spread cost
            net_edge_bps = edge_bps - execution_cost_bps
            executable = net_edge_bps > 0
        else:
            # Predicted inside spread, no executable edge
            net_edge_bps = 0
            executable = False
        
        return {
            "executable": executable,
            "gross_edge_bps": round((predicted_prob - mid) * 10000, 2),
            "execution_cost_bps": round(execution_cost_bps, 2),
            "net_edge_bps": round(net_edge_bps, 2),
            "bid": bid,
            "ask": ask,
            "mid": mid,
            "predicted_prob": predicted_prob,
            "spread_bps": round(spread * 10000, 2)
        }
    
    def analyze_executable_opportunities(self, predicted_probs: Dict[str, List[Tuple[float, float]]]) -> Dict:
        """Analyze executable opportunities given predicted probabilities.
        
        Args:
            predicted_probs: Dict mapping station to list of (strike_temp, predicted_prob) tuples
        
        Returns:
            Analysis of executable opportunities
        """
        liquidity_data = self.analyze_current_liquidity(hours_back=24)
        if "error" in liquidity_data:
            return liquidity_data
        
        opportunities = {}
        
        for station, predictions in predicted_probs.items():
            if station not in liquidity_data["station_breakdown"]:
                continue
                
            station_markets = liquidity_data["station_breakdown"][station].get("markets", [])
            if not station_markets:
                opportunities[station] = {"executable_count": 0, "opportunities": []}
                continue
            
            station_ops = []
            for pred_temp, pred_prob in predictions:
                # Find closest market strikes to evaluate executable edge
                for market in station_markets:
                    strike = market["strike"]
                    bid = market["bid"]
                    ask = market["ask"]
                    
                    # Only analyze if strike is reasonably close to prediction
                    if abs(pred_temp - strike) <= 5:  # Within 5 degree range
                        edge_analysis = self.calculate_executable_edge(bid, ask, pred_prob)
                        if edge_analysis["executable"]:
                            edge_analysis["predicted_temp"] = pred_temp
                            edge_analysis["strike_temp"] = strike
                            edge_analysis["market_ticker"] = market["ticker"]
                            station_ops.append(edge_analysis)
            
            opportunities[station] = {
                "executable_count": len(station_ops),
                "opportunities": station_ops
            }
        
        return {
            "predicted_stations": len(predicted_probs),
            "with_executable": sum(1 for v in opportunities.values() if v["executable_count"] > 0),
            "total_executable": sum(v["executable_count"] for v in opportunities.values()),
            "station_analysis": opportunities
        }


def main():
    parser = argparse.ArgumentParser(description="H.6 — Marketable Strikes and Net Executable Edge Analyzer")
    parser.add_argument("--hours", type=int, default=24, help="Hours back to analyze liquidity")
    parser.add_argument("--predict-station", type=str, help="Station to analyze for executable opportunities")
    parser.add_argument("--predict-temp", type=float, help="Predicted temperature for executable analysis")
    parser.add_argument("--predict-prob", type=float, help="Predicted probability for executable analysis")
    parser.add_argument("--output", type=str, help="Output file for results")
    
    args = parser.parse_args()
    
    analyzer = MarketableStrikeAnalyzer()
    
    if args.predict_station and args.predict_temp is not None and args.predict_prob is not None:
        # Analyze executable opportunities for specific prediction
        predictions = {args.predict_station: [(args.predict_temp, args.predict_prob)]}
        results = analyzer.analyze_executable_opportunities(predictions)
        
        LOGGER.info(f"Executable opportunity analysis for {args.predict_station}:")
        LOGGER.info(json.dumps(results, indent=2, default=str))
    else:
        # Analyze general market liquidity
        results = analyzer.analyze_current_liquidity(hours_back=args.hours)
        
        LOGGER.info(f"Liquidity analysis for last {args.hours} hours:")
        LOGGER.info(f"Active stations: {results.get('active_stations', 0)}/{results.get('total_stations', 0)}")
        
        # Print summary by station
        for station, data in results.get('station_breakdown', {}).items():
            if data['status'] == 'active':
                LOGGER.info(f"  {station}: {data['market_count']} markets, "
                           f"vol: {data['total_volume_24h']:.1f}, "
                           f"OI: {data['total_open_interest']:.1f}")
    
    # Save to file if requested
    if args.output:
        with open(args.output, 'w') as f:
            json.dump(results, f, indent=2, default=str)
        LOGGER.info(f"Results saved to {args.output}")
    
    return 0


if __name__ == "__main__":
    exit(main())