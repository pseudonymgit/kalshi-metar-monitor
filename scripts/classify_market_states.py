#!/usr/bin/env python3
"""
H.7 — All-20 Market-State Classification System

Classifies market states for all 20 stations as:
- Active: Trading with liquidity
- No-market: No corresponding markets exist
- Thin: Low liquidity/trading volume
- Undiscovered: Markets exist but not in our discovery cache
- Paper-only: Markets exist but not suitable for live trading

Focus: Comprehensive market-state awareness for all 20 stations.
"""

import argparse
import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="[H.7] %(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
LOGGER = logging.getLogger("h7_market_state_classifier")


class MarketStateClassifier:
    """Classify market states for all 20 stations."""
    
    def __init__(self, data_dir: str = None):
        self.data_dir = data_dir or os.path.join(os.path.dirname(__file__), "..", "data")
        self.project_root = Path(self.data_dir).parent.absolute()
    
    def get_station_universe(self) -> List[str]:
        """Get the canonical 20-station universe."""
        return [
            "KATL", "KAUS", "KBOS", "KDCA", "KDEN", 
            "KDFW", "KHOU", "KLAS", "KLAX", "KMDW", 
            "KMIA", "KMSP", "KMSY", "KNYC", "KOKC", 
            "KPHL", "KPHX", "KSAT", "KSEA", "KSFO"
        ]
    
    def get_kalshi_snapshot_db_path(self) -> str:
        """Get path to kalshi market snapshots DB."""
        return os.path.join(self.data_dir, "kalshi_market_snapshots.db")
    
    def get_weather_db_paths(self) -> Dict[str, str]:
        """Get paths to all relevant weather data DBs."""
        return {
            "metar": os.path.join(self.data_dir, "metar_backfill.db"),
            "kalshi_settlements": os.path.join(self.data_dir, "kalshi_settlements.db"),
            "kalshi_snapshots": self.get_kalshi_snapshot_db_path(),
            "gefs": os.path.join(self.data_dir, "nwp_forecasts.db"),
            "asos": os.path.join(self.data_dir, "asos_data.db")
        }
    
    def check_station_data_availability(self, station: str) -> Dict[str, bool]:
        """Check if data exists for a station in various DBs."""
        db_paths = self.get_weather_db_paths()
        availability = {}
        
        for db_name, db_path in db_paths.items():
            if not os.path.exists(db_path):
                availability[db_name] = False
                continue
            
            try:
                conn = sqlite3.connect(db_path)
                cursor = conn.cursor()
                
                if db_name == "metar":
                    cursor.execute("SELECT COUNT(*) FROM metar_observations WHERE station_code = ?", (station,))
                    count = cursor.fetchone()[0]
                    availability[db_name] = count > 0
                elif db_name == "kalshi_settlements":
                    cursor.execute("SELECT COUNT(*) FROM settlements WHERE station = ?", (station,))
                    count = cursor.fetchone()[0]
                    availability[db_name] = count > 0
                elif db_name == "kalshi_snapshots":
                    cursor.execute("SELECT COUNT(*) FROM market_snapshots WHERE station = ?", (station,))
                    count = cursor.fetchone()[0]
                    availability[db_name] = count > 0
                elif db_name == "gefs":
                    cursor.execute("SELECT COUNT(*) FROM gefs_forecasts WHERE station = ?", (station,))
                    count = cursor.fetchone()[0]
                    availability[db_name] = count > 0
                elif db_name == "asos":
                    cursor.execute("SELECT COUNT(*) FROM asos_observations WHERE station_code = ?", (station,))
                    count = cursor.fetchone()[0]
                    availability[db_name] = count > 0
                else:
                    availability[db_name] = False
                
                conn.close()
            except Exception as e:
                LOGGER.warning(f"Error checking {db_name} for {station}: {e}")
                availability[db_name] = False
        
        return availability
    
    def classify_kalshi_markets_for_station(self, station: str) -> Dict:
        """Classify Kalshi market state for a specific station."""
        db_path = self.get_kalshi_snapshot_db_path()
        if not os.path.exists(db_path):
            return {"status": "no-market", "reason": "snapshots-db-not-found"}
        
        conn = sqlite3.connect(db_path)
        try:
            # Get most recent snapshots for this station
            cursor = conn.execute("""
                SELECT market_ticker, market_type, yes_bid, yes_ask, mid_price, 
                       volume_24h, open_interest, snapshot_time
                FROM market_snapshots 
                WHERE station = ?
                ORDER BY snapshot_time DESC
                LIMIT 10
            """, (station,))
            
            rows = cursor.fetchall()
            if not rows:
                # Check if markets exist but are just not recently snapped
                cursor = conn.execute("""
                    SELECT DISTINCT market_ticker, market_type
                    FROM market_snapshots 
                    WHERE station = ?
                """, (station,))
                all_markets = cursor.fetchall()
                
                if all_markets:
                    return {
                        "status": "thin", 
                        "reason": "markets-exist-but-no-recent-data",
                        "total_markets": len(all_markets),
                        "market_types": list(set(row[1] for row in all_markets if row[1]))
                    }
                else:
                    return {"status": "no-market", "reason": "no-markets-found"}
            
            # Analyze market liquidity and activity
            active_markets = []
            thin_markets = []
            inactive_markets = []
            
            for row in rows:
                ticker, mtype, bid, ask, mid, vol, oi, snapshot_time = row
                
                # Determine market state based on liquidity indicators
                has_quotes = bid is not None and ask is not None
                has_volume = vol is not None and vol > 0
                has_oi = oi is not None and oi > 0
                
                market_data = {
                    "ticker": ticker,
                    "type": mtype,
                    "has_quotes": has_quotes,
                    "has_volume": has_volume,
                    "has_oi": has_oi,
                    "volume_24h": vol,
                    "open_interest": oi,
                    "snapshot_time": snapshot_time
                }
                
                if has_quotes and (has_volume or has_oi):
                    active_markets.append(market_data)
                elif has_quotes:
                    thin_markets.append(market_data)
                else:
                    inactive_markets.append(market_data)
            
            # Determine overall station state
            if active_markets:
                return {
                    "status": "active",
                    "reason": "markets-with-liquidity",
                    "active_count": len(active_markets),
                    "thin_count": len(thin_markets),
                    "inactive_count": len(inactive_markets),
                    "sample_markets": active_markets[:3]  # Show sample of active markets
                }
            elif thin_markets:
                return {
                    "status": "thin",
                    "reason": "markets-without-volume",
                    "active_count": 0,
                    "thin_count": len(thin_markets),
                    "inactive_count": len(inactive_markets),
                    "sample_markets": thin_markets[:3]
                }
            else:
                return {
                    "status": "no-market",
                    "reason": "no-active-markets",
                    "active_count": 0,
                    "thin_count": 0,
                    "inactive_count": len(inactive_markets)
                }
                
        except Exception as e:
            LOGGER.error(f"Error analyzing markets for {station}: {e}")
            return {"status": "error", "reason": str(e)}
        finally:
            conn.close()
    
    def classify_all_stations(self) -> Dict:
        """Classify market states for all 20 stations."""
        station_states = {}
        station_universe = self.get_station_universe()
        
        # First, check if Kalshi snapshots DB exists
        snapshots_path = self.get_kalshi_snapshot_db_path()
        if not os.path.exists(snapshots_path):
            LOGGER.warning(f"Kalshi snapshots DB not found at {snapshots_path}")
            # Return all stations as undiscovered if DB doesn't exist
            for station in station_universe:
                station_states[station] = {
                    "state": "undiscovered",
                    "reason": "kalshi-snapshots-db-not-found",
                    "data_availability": self.check_station_data_availability(station),
                    "timestamp": datetime.utcnow().isoformat()
                }
            return {
                "classification_timestamp": datetime.utcnow().isoformat(),
                "total_stations": len(station_universe),
                "station_classifications": station_states,
                "summary": {
                    "active": 0,
                    "no_market": len(station_universe),
                    "thin": 0,
                    "undiscovered": len(station_universe),
                    "paper_only": 0
                }
            }
        
        # Classify each station
        for station in station_universe:
            # First classify the Kalshi market state
            market_classification = self.classify_kalshi_markets_for_station(station)
            
            # Then check data availability for additional context
            data_availability = self.check_station_data_availability(station)
            
            # Determine final state based on market classification
            state = market_classification["status"]
            
            # Additional checks for paper-only classification
            if state == "active":
                # Check if this is a paper-only market (exists but not suitable for live trading)
                # This could be determined by various factors like very low volume, 
                # wide spreads, or other risk criteria
                avg_volume = sum(
                    m.get("volume_24h", 0) for m in market_classification.get("sample_markets", [])
                    if m.get("volume_24h") is not None
                ) / max(len(market_classification.get("sample_markets", [])), 1)
                
                if avg_volume < 1.0:  # Threshold for paper-only classification
                    state = "paper-only"
                    market_classification["reason"] = market_classification["reason"] + "; low-volume"
            
            station_states[station] = {
                "state": state,
                "classification_details": market_classification,
                "data_availability": data_availability,
                "timestamp": datetime.utcnow().isoformat()
            }
        
        # Create summary
        state_counts = {
            "active": sum(1 for v in station_states.values() if v["state"] == "active"),
            "no_market": sum(1 for v in station_states.values() if v["state"] == "no-market"),
            "thin": sum(1 for v in station_states.values() if v["state"] == "thin"),
            "undiscovered": sum(1 for v in station_states.values() if v["state"] == "undiscovered"),
            "paper_only": sum(1 for v in station_states.values() if v["state"] == "paper-only")
        }
        
        return {
            "classification_timestamp": datetime.utcnow().isoformat(),
            "total_stations": len(station_universe),
            "station_classifications": station_states,
            "summary": state_counts
        }
    
    def generate_classification_report(self, classification_data: Dict) -> str:
        """Generate a human-readable classification report."""
        report_lines = []
        report_lines.append("=== All-20 Market-State Classification Report ===")
        report_lines.append(f"Generated: {classification_data['classification_timestamp']}")
        report_lines.append(f"Total stations analyzed: {classification_data['total_stations']}")
        report_lines.append("")
        
        # Summary
        summary = classification_data['summary']
        report_lines.append("--- Summary ---")
        report_lines.append(f"Active: {summary['active']}")
        report_lines.append(f"No Market: {summary['no_market']}")
        report_lines.append(f"Thin Liquidity: {summary['thin']}")
        report_lines.append(f"Undiscovered: {summary['undiscovered']}")
        report_lines.append(f"Paper Only: {summary['paper_only']}")
        report_lines.append("")
        
        # Detailed breakdown
        report_lines.append("--- Detailed Breakdown ---")
        for station, data in classification_data['station_classifications'].items():
            state = data['state']
            details = data['classification_details']
            report_lines.append(f"{station}: {state}")
            
            if state == "active":
                active_count = details.get('active_count', 0)
                thin_count = details.get('thin_count', 0)
                report_lines.append(f"  - {active_count} active markets, {thin_count} thin markets")
                sample_markets = details.get('sample_markets', [])[:2]  # Show first 2 markets
                for market in sample_markets:
                    report_lines.append(f"    - {market['ticker']} (vol: {market.get('volume_24h', 'N/A')})")
            elif state == "thin":
                thin_count = details.get('thin_count', 0)
                report_lines.append(f"  - {thin_count} markets without volume")
            elif state == "no-market":
                report_lines.append(f"  - {details.get('reason', 'No markets found')}")
            elif state == "undiscovered":
                report_lines.append(f"  - {details.get('reason', 'Not discovered')}")
            elif state == "paper-only":
                report_lines.append(f"  - {details.get('reason', 'Low volume or risk factors')}")
            
            report_lines.append("")
        
        return "\n".join(report_lines)


def main():
    parser = argparse.ArgumentParser(description="H.7 — All-20 Market-State Classification System")
    parser.add_argument("--output", type=str, help="Output file for classification results")
    parser.add_argument("--report", action="store_true", help="Generate human-readable report")
    parser.add_argument("--data-dir", type=str, help="Directory containing weather data DBs")
    
    args = parser.parse_args()
    
    classifier = MarketStateClassifier(data_dir=args.data_dir)
    
    LOGGER.info("Starting All-20 market-state classification...")
    classification_data = classifier.classify_all_stations()
    
    if args.report:
        report = classifier.generate_classification_report(classification_data)
        LOGGER.info("\n" + report)
    else:
        LOGGER.info(f"Classification complete:")
        LOGGER.info(f"- Total stations: {classification_data['total_stations']}")
        LOGGER.info(f"- Active: {classification_data['summary']['active']}")
        LOGGER.info(f"- No market: {classification_data['summary']['no_market']}")
        LOGGER.info(f"- Thin: {classification_data['summary']['thin']}")
        LOGGER.info(f"- Undiscovered: {classification_data['summary']['undiscovered']}")
        LOGGER.info(f"- Paper only: {classification_data['summary']['paper_only']}")
    
    # Save to file if requested
    if args.output:
        with open(args.output, 'w') as f:
            json.dump(classification_data, f, indent=2, default=str)
        LOGGER.info(f"Classification results saved to {args.output}")
    
    return 0


if __name__ == "__main__":
    exit(main())