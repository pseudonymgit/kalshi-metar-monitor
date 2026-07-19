#!/usr/bin/env python3
"""
Round Number Anchoring Analysis Script

Compares climatological probability vs. market-implied probability at round-number 
thresholds (80, 85, 90, 95, 100) to identify systematic mispricing.

This addresses Workstream 3: Round-Number Anchoring of the Kalshi API 
Integration project - analyzing how market prices are affected by psychological 
anchoring at round number values.

Version Tag: round_number_anchoring_v1.0
Functionality: round_number_anchoring_signal

Usage:
    python scripts/round_number_anchoring.py --db-path data/metar_backfill.db
    python scripts/round_number_anchoring.py --db-path data/metar_backfill.db --stations KNYC,KLAX --print-summary
"""

import os
import sys
from datetime import datetime, timedelta, timezone
import sqlite3
import json
from typing import List, Dict, Any, Tuple
import statistics
import argparse
import logging
from pathlib import Path

# Import station registry as canonical source
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'core'))
import station_registry

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = REPO_ROOT / "data"
ANCHORING_DIR = DATA_DIR / "round_number_anchoring"

DEFAULT_STATIONS = station_registry._RESEARCH_STATION_CODES if hasattr(station_registry, '_RESEARCH_STATION_CODES') else station_registry.get_all_stations()

ROUND_THRESHOLDS = [80, 85, 90, 95, 100]  # Round number thresholds


def create_anchoring_directory():
    """Create the round-number anchoring directory if it doesn't exist."""
    ANCHORING_DIR.mkdir(parents=True, exist_ok=True)


class RoundNumberAnchoringAnalyzer:
    """
    Analyzer to identify round-number anchoring effects in climate and market data.
    
    Compares historical climatological probability vs. market price at round number thresholds
    looking for systematic biases where market prices deviate significantly from historical norms
    due to "round number anchoring" psychological bias.
    """
    
    def __init__(self, station: str, metar_db_path: str, market_db_path: str = None):
        self.station = station
        self.metar_db_path = metar_db_path
        self.market_db_path = market_db_path  # Would be useful if we had market data
        self.results = {
            'station': station,
            'analysis_date': datetime.now(timezone.utc).isoformat(),
            'round_number_anchoring_v1.0': True
        }
    
    def analyze_climatology_by_thresholds(self) -> Dict[str, Any]:
        """
        Analyze historical climatological probabilities by round number thresholds.
        
        Calculates the historical probability of temperature crossing each threshold
        for this station and compares it to potential market prices.
        """
        logger.info(f"Analyzing climatology by round thresholds for {self.station}")
        
        conn = sqlite3.connect(self.metar_db_path)
        cursor = conn.cursor()
        
        # Get historical temperature data for the station
        cursor.execute("""
            SELECT temp_f FROM metar_observations 
            WHERE station = ? AND temp_f IS NOT NULL
        """, (self.station,))
        
        temps = [row[0] for row in cursor.fetchall()]
        conn.close()
        
        if not temps or len(temps) < 10:
            logger.warning(f"Not enough temperature data for {self.station}")
            return self.results
        
        # Calculate probability of exceeding each round threshold
        climatology_probs = {}
        for threshold in ROUND_THRESHOLDS:
            # Count how many times temperature exceeded threshold
            exceed_count = sum(1 for temp in temps if temp >= threshold)
            prob = exceed_count / len(temps)
            climatology_probs[threshold] = {
                'clim_prob': prob,
                'exceed_count': exceed_count,  
                'sample_size': len(temps),
                'avg_temp': sum(temps) / len(temps),
                'median_temp': statistics.median(temps),
                'std_dev': statistics.stdev(temps) if len(temps) > 1 else 0.0,
                'near_crossing_days': sum(1 for temp in temps if abs(temp - threshold) <= 2.0)  # Within 2°F
            }
        
        self.results['climatology_by_threshold'] = climatology_probs
        
        # Identify potential anchoring effects
        # These are points where the market might overvalue/undervalue outcomes due to round number bias
        
        # Simple measure: find thresholds closest to average temperature
        avg_temp = sum(temps) / len(temps)
        temp_proximity_score = {}
        
        for threshold in ROUND_THRESHOLDS:
            dist_from_avg = abs(avg_temp - threshold)
            # Lower scores for closer thresholds (more likely to be "on the money")
            temp_proximity_score[threshold] = {
                'proximity_score': 1.0 / (dist_from_avg + 1),  # +1 to prevent division by zero
                'actual_distance': dist_from_avg
            }
        
        self.results['temp_proximity_scores'] = temp_proximity_score
        
        # Compute anchoring strength score for round numbers
        # This would be higher where historical probability is close to round numbers
        anchoring_scores = {}
        for threshold, data in climatology_probs.items():
            hist_prob = data['clim_prob']
            
            # Round number anchoring happens where market participants focus on round numbers
            # The strongest anchoring occurs when the historical probability is close to round percentage points
            # e.g., close to 0.70 (70%), 0.75 (75%), 0.80 (80%), etc.
            
            # Measure distance to common round percentages: 50%, 60%, 70%, 75%, 80%, 85%, 90%, 95%
            common_probs = [0.50, 0.60, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95]
            prob_anchoring_strenght = min([abs(hist_prob - cp) for cp in common_probs])
            
            # Also consider proximity to threshold itself
            proximity_multiplier = data['near_crossing_days'] / data['sample_size'] if data['sample_size'] > 0 else 0
            
            anchoring_scores[threshold] = {
                'baseline_strength': 1.0 - prob_anchoring_strenght,  # Higher when closer to round prob
                'proximity_multiplier': proximity_multiplier,  # Higher when many obs are near this temp
                'combined_score': (1.0 - prob_anchoring_strenght) * proximity_multiplier,
                'historical_probability': hist_prob,
                'is_probability_roundish': hist_prob in common_probs,
                'probability_distance_to_nearest_common': prob_anchoring_strenght
            }
        
        self.results['round_number_anchoring_strength'] = anchoring_scores
        
        # Identify potential opportunities based on deviation between clim prob and round thresholds
        anchoring_opportunities = []
        for threshold, anchoring_data in anchoring_scores.items():
            hist_prob_percent = anchoring_data['historical_probability'] * 100
            
            # If a market price is near the round threshold (e.g., 80 or 0.80) but the 
            # historical probability is significantly different, there might be an opportunity
            
            prob_diff = abs(hist_prob_percent - threshold)  # Difference in percentage terms
            
            # Weight by anchoring strength
            opportunity_scoring = prob_diff * anchoring_data['combined_score']
            
            anchoring_opportunities.append({
                'threshold': threshold,
                'historical_probability_percent': round(hist_prob_percent, 2),
                'market_anchor': threshold,  # In ideal market, this would be market price at equilibrium
                'probability_difference': round(prob_diff, 2),  # % diff between history and round number
                'anchoring_strength': round(anchoring_data['combined_score'], 3),
                'opportunity_score': round(opportunity_scoring, 3),
                'sample_size': climatology_probs[threshold]['sample_size'],
                'exceedance_count': climatology_probs[threshold]['exceed_count']
            })
        
        # Sort by opportunity score to highlight the most attractive round number anchor effects
        anchoring_opportunities.sort(key=lambda x: x['opportunity_score'], reverse=True)
        
        self.results['anchoring_opportunities'] = anchoring_opportunities
        
        logger.info(f"Completed anchoring analysis for {self.station}: {len(anchoring_opportunities)} potential opportunities identified")
        
        return self.results


def analyze_multi_station_anchoring(db_path: str, stations: List[str] = DEFAULT_STATIONS) -> Dict[str, Any]:
    """
    Analyze round-number anchoring effects across multiple stations.
    
    Args:
        db_path: Path to the METAR database
        stations: List of ICAO station codes to analyze
    
    Returns:
        Dictionary containing anchoring analysis for all stations
    """
    results = {
        'analysis_timestamp': datetime.now(timezone.utc).isoformat(),
        'stations_analyzed': [],
        'station_anchoring_analysis': {},
        'aggregate_opportunities': [],
        'summary_statistics': {}
    }
    
    logger.info(f"Beginning round-number anchoring analysis for {len(stations)} stations")
    
    all_opportunities = []
    
    for station in stations:
        analyzer = RoundNumberAnchoringAnalyzer(station, db_path)
        try:
            analysis = analyzer.analyze_climatology_by_thresholds()
            
            if 'climatology_by_threshold' in analysis and analysis['climatology_by_threshold']:
                results['station_anchoring_analysis'][station] = analysis   
                results['stations_analyzed'].append(station)
                
                # Collect opportunities across all stations for aggregate analysis
                if 'anchoring_opportunities' in analysis:
                    station_ops = analysis['anchoring_opportunities']
                    for op in station_ops:
                        op_with_station = op.copy()
                        op_with_station['station'] = station
                        all_opportunities.append(op_with_station)
                
                logger.info(f"Completed anchoring analysis for {station}")
            else:
                logger.info(f"No sufficient data for anchoring analysis for {station}")
        except Exception as e:
            logger.error(f"Error analyzing {station}: {e}")
    
    # Add all opportunities to aggregate list
    results['aggregate_opportunities'] = all_opportunities
    
    # Create summary statistics
    total_ops = len(all_opportunities)
    if total_ops > 0:
        avg_opp_score = sum(op['opportunity_score'] for op in all_opportunities) / total_ops
        high_opp_count = sum(1 for op in all_opportunities if op['opportunity_score'] > 0.5)
        
        results['summary_statistics'] = {
            'total_opportunities_identified': total_ops,
            'average_opportunity_score': round(avg_opp_score, 3),
            'high_quality_opportunities': high_opp_count,  # Score > 0.5
            'most_common_round_numbers': {}
        }
        
        # Count occurrences by round number
        round_number_counts = {}
        for op in all_opportunities:
            rn = op['threshold']
            round_number_counts[rn] = round_number_counts.get(rn, 0) + 1
            
        results['summary_statistics']['most_common_round_numbers'] = dict(round_number_counts)
    
    logger.info(f"Anchoring analysis complete: {len(results['station_anchoring_analysis'])} stations, {len(all_opportunities)} total opportunities")
    
    return results


def generate_anchoring_signals(analysis_results: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Generate signals based on the round number anchoring analysis.
    
    These signals indicate potential market inefficiencies where round number anchoring
    may have caused systematic mispricing.
    """
    signals = []
    
    for station, analysis in analysis_results.get('station_anchoring_analysis', {}).items():
        opportunities = analysis.get('anchoring_opportunities', [])
        
        # Filter to only high-value anchoring opportunities
        strong_opps = [op for op in opportunities if op['opportunity_score'] > 0.3]
        
        for opp in strong_opps:
            if opp['probability_difference'] > 5.0:  # At least 5% diff
                # Determine trade direction based on relative value
                # If hist prob > market anchor, then market "undervalues" up moves - potentially good to buy YES contracts
                is_overpriced = opp['historical_probability_percent'] < opp['market_anchor']
                
                signal = {
                    'trade_version': 'round_number_anchoring_v1.0',
                    'functionality': 'round_number_anchoring_signal',
                    'station': station,
                    'signal_type': 'round_number_anchoring',
                    'threshold': opp['threshold'],
                    'historical_probability_percent': opp['historical_probability_percent'],
                    'market_anchor_percent': opp['market_anchor'],
                    'probability_difference_percentage': opp['probability_difference'],
                    'opportunity_score': opp['opportunity_score'],
                    'sample_size': opp['sample_size'],
                    'recommendation_direction': 'MARKET_OVERPRICED' if is_overpriced else 'MARKET_UNDERPRICED',
                    'trading_implication': 'Sell YES contracts if overpriced, Buy YES contracts if underpriced',
                    'timestamp_utc': datetime.now(timezone.utc).isoformat()
                }
                signals.append(signal)
    
    logger.info(f"Generated {len(signals)} round-number anchoring signals")
    return signals


def save_anchoring_results(results: Dict[str, Any], output_file: str = None):
    """
    Save the anchoring analysis results to a JSON file.
    
    Args:
        results: Dictionary containing results from analysis
        output_file: Specific file path to save to, or auto-generate
    """
    if not output_file:
        timestamp = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
        output_file = f"round_number_anchoring_analysis_{timestamp}.json"
    
    output_path = ANCHORING_DIR / output_file
    
    with open(output_path, 'w') as f:
        json.dump(results, f, indent=2, default=str)
    
    logger.info(f"Anchoring analysis results saved to {output_path}")
    return str(output_path)


def main():
    """
    Command-line interface for the Round Number Anchoring Analyzer.
    
    Example usage:
        python round_number_anchoring.py --db-path /path/to/db.sqlite
        python round_number_anchoring.py --db-path /path/to/db.sqlite --stations KJFK,KLAX
    """
    parser = argparse.ArgumentParser(description='Round Number Anchoring Analysis for Weather Markets')
    parser.add_argument('--db-path', type=str, 
                       default=str(DATA_DIR / 'metar_backfill.db'),
                       help='Path to the METAR database (default: data/metar_backfill.db)')
    parser.add_argument('--stations', type=str,
                       help='Comma-separated list of station codes to analyze (default: all)')
    parser.add_argument('--output', type=str,
                       help='Output file name (default: auto-generated)')
    parser.add_argument('--print-summary', action='store_true',
                       help='Print a summary of the anchoring analysis')
    
    args = parser.parse_args()
    
    # Create necessary directories
    create_anchoring_directory()
    
    # Determine stations list
    if args.stations:
        stations = [s.strip().upper() for s in args.stations.split(',')]
    else:
        stations = DEFAULT_STATIONS
    
    # Verify DB exists
    if not os.path.exists(args.db_path):
        logger.error(f"Database file not found: {args.db_path}")
        sys.exit(1)
    
    # Perform analysis
    results = analyze_multi_station_anchoring(args.db_path, stations)
    
    # Generate signals based on analysis
    signals = generate_anchoring_signals(results)
    results['signals'] = signals
    
    # Save results
    save_path = save_anchoring_results(results, args.output)
    
    # Print summary if requested
    if args.print_summary:
        print("\nROUND-NUMBER ANCHORING ANALYSIS SUMMARY")
        print("=" * 50)
        print(f"Analysis Timestamp: {results['analysis_timestamp']}")
        print(f"Stations Analyzed: {len(results['stations_analyzed'])}")
        stats = results.get('summary_statistics', {})
        print(f"Total Opportunities Identified: {stats.get('total_opportunities_identified', 0)}")
        print(f"Avg Opportunity Score: {stats.get('average_opportunity_score', 0)}")
        print(f"High-Quality Opportunities: {stats.get('high_quality_opportunities', 0)}")
        print(f"Total Signals Generated: {len(signals)}")
        
        print("\nRound Number Activity:")
        round_counts = stats.get('most_common_round_numbers', {})
        if round_counts:
            for rn, count in sorted(round_counts.items(), key=lambda x: x[1], reverse=True):
                print(f"  {rn}°F threshold: {count} significant opportunities")
        
        # Show a sample of the highest scoring opportunities
        opportunities = []
        for station_analysis in results['station_anchoring_analysis'].values():
            opportunities.extend(station_analysis.get('anchoring_opportunities', []))
        
        if opportunities:
            print(f"\nTop 5 Highest-Impact Anchoring Opportunities:")
            top_ops = sorted(opportunities, key=lambda x: x['opportunity_score'], reverse=True)[:5]
            for i, opp in enumerate(top_ops, 1):
                print(f"  {i}. {opp['station']} @ {opp['threshold']}°F: Hist={opp['historical_probability_percent']}%, Diff={opp['probability_difference']}%, Score={opp['opportunity_score']}")

        print(f"\nDetailed results saved to: {save_path}")
    
    return 0


if __name__ == "__main__":
    sys.exit(main())