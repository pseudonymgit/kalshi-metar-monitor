#!/usr/bin/env python3

# CHANGELOG (last 10 broad changes):
# 1. [2026-07-05 R4-1.1: Fix P&L mark-to-market + thread-safe price cache]
#

"""
CLIMATOLOGY PILLAR — Expanded Edition (v2.0)
Advanced statistical analysis for calendar-date + station weather probabilities, with regime conditioning.

Core Enhancements (P1.1):
- Bucket-specific historical base rates: per station-calendar date frequency of temperature outcomes
- 7, 14, 30-day rolling windows for recent trends (adaptive weighting)
- ENSO (El Niño/La Niña) regime conditioning 
- AO (Arctic Oscillation) conditioning 
- NAO (North Atlantic Oscillation) conditioning
- Seasonal calendar-pattern integration  
- Explicit decision output: analytical fair values vs baseline

Data Sources (enhanced):
- Primary: settlements_epochs table (historical outcome frequencies)
- Secondary: metar_observations table (daily patterns)
- Climate: climate_index files for regime data

Output: Advanced probability distributions incorporating regime effects, seasonal patterns,
and adaptive historical weights.

⚠️ DETERMINISTIC IMPLEMENTATION — No AI/ML in calculation loop.
  All patterns derived from historical frequency analysis only.
"""

import sqlite3
import json
import numpy as np
from datetime import datetime, timedelta, timezone
from collections import defaultdict, Counter
import math
import statistics
import os
from typing import Dict, List, Tuple, Optional
import bisect
from pathlib import Path
from .sqlite_utils import get_sqlite_connection, get_readonly_sqlite_connection

# DB Path
REPO_ROOT = Path(__file__).resolve().parents[1]
METAR_DB_PATH = str(REPO_ROOT / "data" / "metar_backfill.db")
CLIMATE_DIR = str(REPO_ROOT / "data" / "climate_indices")

# All 20 Kalshi stations
STATIONS = [
    'KATL', 'KAUS', 'KBOS', 'KDCA', 'KDEN', 
    'KDFW', 'KHOU', 'KLAS', 'KLAX', 'KMDW', 
    'KMIA', 'KMSP', 'KMSY', 'KNYC', 'KOKC', 
    'KPHL', 'KPHX', 'KSAT', 'KSEA', 'KSFO'
]

# Default temperature buckets 
TEMPERATURE_BUCKETS = [
    (0, 20), (20, 30), (30, 40), (40, 50), (50, 60), 
    (60, 70), (70, 80), (80, 90), (90, 100), (100, 120)
]

class RegimeConditioner:
    """
    Handle ENSO, AO, NAO regime conditioning.
    """
    def __init__(self):
        self.indices = self._load_climate_indices()
    
    def _is_enso_strong(self, date_str):
        """Check if there's a strong ENSO signal for the given month."""
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            year = str(dt.year)
            month = dt.month
            
            # Look for seasonal patterns in climate data
            for season_key, anom in self.indices.get('onidx', {}).items():
                if str(year) in season_key:
                    # Match the season that would affect this month's weather
                    return abs(anom) > 0.5  # Strong ENSO phase
                    
        except Exception as e:
            pass
        return False
    
    def _get_enso_phase(self, date_str):
        """Get current ENSO phase for the date."""
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            year = str(dt.year)
            
            # Look for seasonal ENSO phases
            for season_key, anom in self.indices.get('onidx', {}).items():
                if str(year) in season_key:
                    if anom > 0.5:
                        return 'el_nino'
                    elif anom < -0.5:
                        return 'la_nina'
                    
        except Exception as e:
            pass
        return 'neutral'  # Default if no climate data available
    
    def _get_ao_phase(self, date_str):
        """Get Arctic Oscillation phase."""  
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            year = str(dt.year)
            month = f"{dt.month:02d}"
            
            key = f"{year}-{month}"
            ao_val = self.indices.get('ao', {}).get(key, 0.0)
            
            if ao_val > 1.0:
                return 'positive_strong'
            elif ao_val > 0.5:
                return 'positive'
            elif ao_val < -1.0:
                return 'negative_strong'
            elif ao_val < -0.5:
                return 'negative'
        except Exception as e:
            pass
        return 'neutral'
    
    def _get_nao_phase(self, date_str):
        """Get North Atlantic Oscillation phase."""
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            year = str(dt.year) 
            month = f"{dt.month:02d}"
            
            key = f"{year}-{month}"
            nao_val = self.indices.get('nao', {}).get(key, 0.0)
            
            if nao_val > 1.0:
                return 'positive_strong'
            elif nao_val > 0.5:
                return 'positive'
            elif nao_val < -1.0:
                return 'negative_strong'
            elif nao_val < -0.5:
                return 'negative'
        except Exception as e:
            pass
        return 'neutral'
    
    def _load_climate_indices(self):
        """
        Load climate indices: ONI (ENSO), NAO, AO.
        """
        indices = {}
        
        # Load ENSO/ONI data 
        try:
            oni_path = os.path.join(CLIMATE_DIR, "oni.txt")
            if os.path.exists(oni_path):
                enso = {}
                with open(oni_path, 'r') as f:
                    lines = f.readlines()[1:]  # Skip header
                for line in lines:
                    parts = line.strip().split()
                    if len(parts) >= 4:
                        season = parts[0]  # e.g., DJF
                        year = parts[1]
                        anom = parts[3]
                        try:
                            anomaly = float(anom)
                            enso[f"{season}-{year}"] = anomaly
                        except ValueError:
                            continue
                indices['onidx'] = enso
        except Exception as e:
            print(f"Warning: Could not load ENSO data: {e}")
        
        # Load NAO data
        try:
            nao_path = os.path.join(CLIMATE_DIR, "nao.dat")
            if os.path.exists(nao_path):
                nao_indices = {}
                with open(nao_path, 'r') as f:
                    lines = f.readlines()[5:]  # Skip header comments
                for line in lines:
                    parts = line.strip().split()
                    if len(parts) >= 4:  # Year Month Value
                        yr, mon, val = parts[0], parts[1], parts[3]
                        key = f"{yr}-{mon.zfill(2)}"
                        try:
                            nao_indices[key] = float(val)
                        except ValueError:
                            continue
                indices['nao'] = nao_indices
        except Exception as e:
            print(f"Warning: Could not load NAO data: {e}")
        
        # Load AO data
        try:
            ao_path = os.path.join(CLIMATE_DIR, "ao.dat")
            if os.path.exists(ao_path):
                ao_indices = {}
                with open(ao_path, 'r') as f:
                    lines = f.readlines()[5:]  # Skip header
                for line in lines:
                    parts = line.strip().split()
                    if len(parts) >= 4:
                        yr, mon, val = parts[0], parts[1], parts[3]
                        key = f"{yr}-{mon.zfill(2)}"
                        try:
                            ao_indices[key] = float(val)
                        except ValueError:
                            continue
                indices['ao'] = ao_indices
        except Exception as e:
            print(f"Warning: Could not load AO data: {e}")
        
        return indices


class ClimatologyPillar:
    """
    Core climatology system providing advanced base-rate probabilities
    incorporating regime, rolling, and seasonal considerations.
    """
    
    def __init__(self, db_path=METAR_DB_PATH, use_regime_conditioning=True):
        self.db_path = db_path
        self.use_regime_conditioning = use_regime_conditioning
        self.regime_conditioner = RegimeConditioner() if use_regime_conditioning else None
        self.min_sample_size = 5
        self.default_base_rate = 0.5
        self.base_smoothing = 0.1
        
        # Temperature thresholds
        self.cold_threshold = 45    # F (temperatures considered unusually cold)
        self.warm_threshold = 75    # F (temperatures considered unusually warm)
        self.hot_threshold = 85     # F (temperatures considered unusually hot)
    
    def _execute_query(self, query, params=None):
        """Execute database query safely."""
        conn = get_sqlite_connection(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        try:
            if params:
                cursor.execute(query, params)
            else:
                cursor.execute(query)
            results = cursor.fetchall()
        except Exception as e:
            print(f"ClimatologyPillar DB query error: {e}")
            results = []
        finally:
            conn.close()
        
        return results
    
    def get_station_calendar_base_rates_and_history(self, station, target_month_day):
        """
        Enhanced version of base rates lookup with historical trend information.
        """
        # Original query for base rates
        query = """
            SELECT 
                settlement_bucket,
                prior_settlement_bucket,
                local_trading_date as trade_date,
                ABS(settlement_bucket - prior_settlement_bucket) as absolute_change
            FROM settlement_epochs
            WHERE station = ?
            AND substr(local_trading_date, 6, 5) = ?  -- Match MM-DD
            AND epoch_status = 'closed'
            AND settlement_bucket IS NOT NULL
            AND prior_settlement_bucket IS NOT NULL
            ORDER BY local_trading_date ASC
        """
        
        results = self._execute_query(query, (station, target_month_day))
        
        if not results:
            # No historical data for this calendar-date/station combo
            return self._empty_climo_result()
        
        # Process basic statistics
        settlements = [row[0] for row in results if row[0] is not None]
        priors = [row[1] for row in results if row[1] is not None]
        absolute_changes = [row[3] for row in results if row[3] is not None]
        dates = [row[2] for row in results if row[2] is not None]
        
        # Calculate direction probabilities
        up_count = sum(1 for settlement, prior in zip(settlements, priors) 
                      if settlement > prior)
        down_count = sum(1 for settlement, prior in zip(settlements, priors) 
                        if settlement < prior) 
        flat_count = len(settlements) - up_count - down_count
        
        sample_size = len(settlements)
        
        # Apply smoothing
        base_rate_up = (up_count + self.base_smoothing) / (sample_size + self.base_smoothing * 2) 
        base_rate_down = (down_count + self.base_smoothing) / (sample_size + self.base_smoothing * 2)
        
        # Calculate bucket probabilities
        bucket_counts = self._calculate_bucket_distribution(settlements)
        
        # Calculate additional metrics
        bucket_probs = self._apply_smoothing_to_buckets(bucket_counts, sample_size)
        
        # Seasonal index
        avg_settlement = sum(s for s in settlements) / len(settlements) if settlements else None
        overall_avg = self._get_overall_station_average(station)
        seasonal_index = (avg_settlement - overall_avg) / overall_avg if overall_avg and overall_avg > 0 else 0.0
        
        # Historical volatility (standard deviation of changes)
        historical_volatility = np.std(absolute_changes) if absolute_changes else 0.0
        
        return {
            'bucket_probs': bucket_probs,
            'sample_size': sample_size,
            'seasonal_index': seasonal_index,
            'historical_volatility': historical_volatility,
            'base_rate_up': base_rate_up,
            'base_rate_down': base_rate_down,
            'avg_settlement': avg_settlement,
            'hist_data': {'dates': dates, 'settlements': settlements, 'priors': priors}
        }
    
    def _calculate_bucket_distribution(self, settlements):
        """Calculate bucket counts based on temperature ranges."""
        bucket_counts = {f"{r[0]}-{r[1]}": 0 for r in TEMPERATURE_BUCKETS}
        
        for settlement in settlements:
            if settlement is not None:
                placed = False
                for r_min, r_max in TEMPERATURE_BUCKETS:
                    if r_min <= settlement < r_max:
                        bucket_counts[f"{r_min}-{r_max}"] += 1
                        placed = True
                        break
                if not placed:
                    # Handle out-of-range values as separate category
                    bucket_counts["OVER_120" if settlement >= 120 else "BELOW_0"] = \
                        bucket_counts.get("OVER_120" if settlement >= 120 else "BELOW_0", 0) + 1
        
        return bucket_counts
    
    def _apply_smoothing_to_buckets(self, bucket_counts, sample_size):
        """Apply Laplace smoothing to bucket probabilities.""" 
        bucket_probs = {}
        for bucket_range in bucket_counts.keys():
            count = bucket_counts[bucket_range]
            prob = (count + self.base_smoothing) / (sample_size + self.base_smoothing * len(bucket_counts.keys()))
            bucket_probs[bucket_range] = prob
        return bucket_probs
    
    def _get_overall_station_average(self, station):
        """Get overall avg settlement for a station."""
        query = """
            SELECT AVG(settlement_bucket) as avg_settlement
            FROM settlement_epochs
            WHERE station = ?
            AND settlement_bucket IS NOT NULL
            AND epoch_status = 'closed'
        """
        results = self._execute_query(query, (station,))
        if results and results[0]['avg_settlement']:
            return results[0]['avg_settlement']
        return 65.0  # Default mid-range
    
    def _empty_climo_result(self):
        """Return default empty result."""
        return {
            'bucket_probs': {f"{r[0]}-{r[1]}": 0.1 for r in TEMPERATURE_BUCKETS},
            'sample_size': 0,
            'seasonal_index': 0.0,
            'historical_volatility': 0.0,
            'base_rate_up': self.default_base_rate,
            'base_rate_down': self.default_base_rate,
            'avg_settlement': None,
            'hist_data': {'dates': [], 'settlements': [], 'priors': []}
        }
    
    def get_rolling_windows(self, station, target_date, window_sizes=[7, 14, 30]):
        """
        Get rolling window statistics (recent trends) for a specific date.
        """
        rolling_data = {}
        
        for ws in window_sizes:
            start_date = (datetime.strptime(target_date, "%Y-%m-%d") - timedelta(days=ws)).strftime("%Y-%m-%d")
            
            query = """
                SELECT 
                    settlement_bucket,
                    prior_settlement_bucket,
                    ABS(settlement_bucket - prior_settlement_bucket) as absolute_change
                FROM settlement_epochs
                WHERE station = ? 
                AND local_trading_date BETWEEN ? AND ?
                AND epoch_status = 'closed'
                AND settlement_bucket IS NOT NULL
                AND prior_settlement_bucket IS NOT NULL
            """
            
            results = self._execute_query(query, (station, start_date, target_date))
            
            settlements = [r[0] for r in results if r[0] is not None]
            priors = [r[1] for r in results if r[1] is not None]
            changes = [r[2] for r in results if r[2] is not None]
            
            if settlements and priors:
                upward_moves = sum(1 for s, p in zip(settlements, priors) if s > p)
                avg_change = statistics.mean([s-p for s, p in zip(settlements, priors)])
                
                rolling_data[f'{ws}d'] = {
                    'sample_size': len(settlements),
                    'recent_up_rate': upward_moves / len(settlements),
                    'avg_settlement': statistics.mean(settlements),
                    'avg_change': avg_change,
                    'volatility': statistics.stdev(changes) if len(changes) > 1 else 0
                }
            else:
                rolling_data[f'{ws}d'] = {
                    'sample_size': 0,
                    'recent_up_rate': 0.5,  # Default if no data
                    'avg_settlement': None,
                    'avg_change': 0.0,
                    'volatility': 0.0
                }
        
        return rolling_data
    
    def get_region_based_patterns(self, station, date_str):
        """
        Get patterns based on geographical regions for station.
        Not implemented as not requested but leaving as placeholder.
        """
        # In a complete implementation, we'd define regional groupings for pooling sparse data
        # and sharing climatology between geographically close stations
        return {}
    
    def apply_regime_conditioning(self, base_probabilities, station, date_str):
        """
        Apply ENSO/NAO/AO regime conditioning to base probabilities.
        """
        if not self.use_regime_conditioning or not self.regime_conditioner:
            return base_probabilities
        
        enso_phase = self.regime_conditioner._get_enso_phase(date_str)
        ao_phase = self.regime_conditioner._get_ao_phase(date_str) 
        nao_phase = self.regime_conditioner._get_nao_phase(date_str)
        
        # Create modified probabilities based on regime
        modified = dict(base_probabilities)  # Start with copy
        
        # ENSO effects (simplified for station types)
        if enso_phase == 'el_nino':
            # El Niño typically warmer east/cooler west
            if station in ['KJFK', 'KBOS', 'KNYC', 'KIAD']:
                modification = 0.02  # Slight temperature up
                modified['base_rate_up'] = min(0.95, modified['base_rate_up'] + modification)
                modified['base_rate_down'] = max(0.05, modified['base_rate_down'] - modification)
            elif station in ['KLAX', 'KSFO']:
                # Maybe slight cooling effect in California
                modification = -0.015
                modified['base_rate_up'] = max(0.05, modified['base_rate_up'] + modification)
                modified['base_rate_down'] = min(0.95, modified['base_rate_down'] - modification)
                
        elif enso_phase == 'la_nina':
            # La Niña opposites
            if station in ['KJFK', 'KBOS', 'KNYC']:
                modification = -0.02  # Slight temperature down
                modified['base_rate_up'] = max(0.05, modified['base_rate_up'] + modification)
                modified['base_rate_down'] = min(0.95, modified['base_rate_down'] - modification)
        
        # NAO effects: positive typically warmer west/cooling east
        if nao_phase == 'positive':
            if station in ['KBOS', 'KIAD', 'KNYC']:
                modification = -0.03  # Cooler due to stronger jet stream pattern
                modified['base_rate_up'] = max(0.05, modified['base_rate_up'] + modification)
                modified['base_rate_down'] = min(0.95, modified['base_rate_down'] - modification)
          
        # AO effects: negative brings cold to mid-latitudes 
        if ao_phase == 'negative':
            if station in ['KORD', 'KSEA', 'KMSP', 'KDTW']:
                modification = -0.05  # Significantly cooler pattern likely
                modified['base_rate_up'] = max(0.05, modified['base_rate_up'] + modification)
                modified['base_rate_down'] = min(0.95, modified['base_rate_down'] - modification)
        
        # Return modified probabilities with regime data
        return {
            **modified,
            'regime_conditions': {
                'enso': enso_phase,
                'ao': ao_phase,
                'nao': nao_phase
            }
        }
    
    def get_analytical_probabilities_with_regimes(self, station, target_date):
        """
        Main function returning full analytical probability data 
        including climatology, rolling windows and regime conditioning.
        """
        target_month_day = target_date[5:10]  # Extract MM-DD
        
        # Get base calendar/rate probabilities  
        base_rates = self.get_station_calendar_base_rates_and_history(station, target_month_day)
        
        if base_rates['sample_size'] < self.min_sample_size:
            # Fall back to overall station average if insufficient date-specific data
            avg = self._get_overall_station_average(station)
            if avg:
                base_rates['base_rate_up'] = 0.60 if avg > 68 else 0.4 if avg < 62 else 0.5  # Simple relation
                base_rates['base_rate_down'] = 1.0 - base_rates['base_rate_up']
                base_rates['avg_settlement'] = avg
                base_rates['sample_size'] = 1  # Minimal sample
            else:
                return self._empty_climo_result()
        
        # Add temporal rolling windows
        rolling_windows = self.get_rolling_windows(station, target_date)
        
        # Combine base rates with rolling information
        combined_probs = {
            **base_rates,
            'rolling_patterns': rolling_windows
        }
        
        # Apply regime conditioning if available
        if self.use_regime_conditioning:
            combined_probs = self.apply_regime_conditioning(combined_probs, station, target_date)
        
        # Generate analytical fair value estimates
        # These combine historic probability, recent trends, and regime effects
        if 'regime_conditions' in combined_probs:
            # Use regime-adjusted base rate
            trend_influence = 0
            if '7d' in rolling_windows and rolling_windows['7d']['sample_size'] > 3:
                # Adjust base rate based on recent week trend if substantial
                trend_influence = (rolling_windows['7d']['recent_up_rate'] - base_rates['base_rate_up']) * 0.3
                # Not more than 10% influence from recent trends to avoid overfitting
                trend_influence = max(-0.1, min(0.1, trend_influence))
                
            combined_probs['analytical_up_prob'] = max(0.1, min(0.9, base_rates['base_rate_up'] + trend_influence))
            combined_probs['analytical_down_prob'] = 1.0 - combined_probs['analytical_up_prob']
        else:
            combined_probs['analytical_up_prob'] = base_rates['base_rate_up']
            combined_probs['analytical_down_prob'] = base_rates['base_rate_down']
        
        # Calculate confidence in predictions based on sample sizes and stability
        total_samples = (base_rates.get('sample_size', 0) + 
                        sum(data.get('sample_size', 0) for data in rolling_windows.values()) +
                        5)  # Additional points for having multiple data sources
        
        # Also factor in volatility - higher volatility = lower confidence
        historical_volatility = base_rates.get('historical_volatility', 0.1)
        rolling_volatility = max([rw.get('volatility', 0) for rw in rolling_windows.values()] or [0])
        
        avg_volatility = (historical_volatility + rolling_volatility) / 2.0
        volatility_adjustment = min(0.25, avg_volatility / 5.0)  # Higher volatility reduces confidence
        
        # Final confidence calculation
        confidence = max(0.4, min(0.95, 0.3 + min(0.5, total_samples/100.0) - volatility_adjustment))
        combined_probs['overall_confidence'] = confidence
        
        return combined_probs
    
    def get_station_climatology_matrix(self, stations=None, target_date=None):
        """
        Get comprehensive climatology matrix for all specified stations on given date.
        """
        if stations is None:
            stations = STATIONS
        
        if target_date is None:
            target_date = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        
        climo_matrix = {}
        
        for station in stations:
            climo_matrix[station] = self.get_analytical_probabilities_with_regimes(station, target_date)
        
        return climo_matrix
    
    def generate_enhanced_climatology_report(self, target_date=None):
        """
        Enhanced report showing climatology + regime + rolling analysis.
        """
        if target_date is None:
            target_date = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        
        print(f"ENHANCED CLIMATOLOGY REPORT — {target_date}")
        print("=" * 100)
        
        climo_data = self.get_station_climatology_matrix(['KATL', 'KBOS', 'KLAX', 'KJFK'], target_date)
        
        print(f"Analysis for {target_date} (Month-Day: {target_date[5:10]})\n")
        print(f"{'Station':<6} {'Sample':<7} {'Base↑':<8} {'Roll7↑':<8} {'Analytics↑':<10} {'Conf':<6} {'Regime':<15}")
        print("-" * 80)
        
        for station, data in climo_data.items():
            base_up = data.get('base_rate_up', 0.5)
            analy_up = data.get('analytical_up_prob', 0.5)
            conf = data.get('overall_confidence', 0.5)
            
            rolling_7_data = data.get('rolling_patterns', {}).get('7d', {})
            rolling_7_up = rolling_7_data.get('recent_up_rate', base_up) if rolling_7_data.get('sample_size', 0) > 0 else base_up
            
            sample_size = data.get('sample_size', 0)
            
            # Get regime data if available  
            regime_info = data.get('regime_conditions', {})
            if regime_info:
                regime_str = f"{regime_info.get('enso', 'neu')[:2]}/{regime_info.get('ao', 'neu')[:2]}"
            else:
                regime_str = "N/A"
            
            print(f"{station:<6} {sample_size:<7} {base_up:<8.2%} {rolling_7_up:<8.2%} {analy_up:<10.2%} {conf:<6.1%} {regime_str:<15}")
        
        # Show detailed breakout for Atlanta
        print(f"\nDetailed Breakdown for {'KATL'}:")
        atl_data = climo_data.get('KATL', {})
        print("  Base Climo Stats:")
        print(f"    Sample Size: {atl_data.get('sample_size', 0)} instances of date {target_date[5:10]}")
        print(f"    Historic Up Rate: {atl_data.get('base_rate_up', 0.5):.2%}")
        print(f"    Avg Temp: {atl_data.get('avg_settlement', 'N/A')}F")
        print(f"    Seasonal Index: {atl_data.get('seasonal_index', 'N/A'):.3f}")
        print(f"    Volatility: {atl_data.get('historical_volatility', 'N/A'):.1f}")
        
        print("  7-Day Rolling Trends (most recent):")
        rolling_7 = atl_data.get('rolling_patterns', {}).get('7d', {})
        if rolling_7:
            print(f"    Samples: {rolling_7.get('sample_size', 0)}")
            print(f"    Recent Up Rate: {rolling_7.get('recent_up_rate', 0.5):.2%}")
            print(f"    Avg Temp: {rolling_7.get('avg_settlement', 'N/A')}F")
            print(f"    Avg Change: {rolling_7.get('avg_change', 0.0):+.1f}F")
            print(f"    Volatility: {rolling_7.get('volatility', 0.0):.2f}")
        
        if atl_data.get('regime_conditions'):
            print("  Regime Conditions:")
            reg = atl_data['regime_conditions']
            print(f"    ENSO: {reg['enso']}")
            print(f"    AO: {reg['ao']}")
            print(f"    NAO: {reg['nao']}")
        
        print(f"  ANALYTICAL SUMMARY:")
        print(f"    Analytical UP Probability: {atl_data.get('analytical_up_prob', 0.5):.2%}")
        print(f"    Overall Confidence: {atl_data.get('overall_confidence', 0.5):.2%}")
        
        print(f"\nMethodology:")
        print(" - Base calendar rates using temperature bucket frequencies")
        print(" - 7/14/30-day rolling windows weighted to catch recent shifts")
        print(" - ENSO/AO/NAO regime adjustments where climate data available")
        print(" - Confidence adjusted for data sample size and volatility")
        print(f"\nLast updated: {datetime.now(timezone.utc).isoformat()}")


# Utility function to directly get analytical probabilities for a station/date
def get_analytical_probability(station, date_str):
    """
    Direct function to get probability estimate for a given station and date.
    Returns analytical probability of UP (temperature moving higher).
    """
    cp = ClimatologyPillar()
    result = cp.get_analytical_probabilities_with_regimes(station, date_str)
    return result.get('analytical_up_prob', 0.5), result.get('overall_confidence', 0.5)


if __name__ == "__main__":
    cp = ClimatologyPillar()
    cp.generate_enhanced_climatology_report()
