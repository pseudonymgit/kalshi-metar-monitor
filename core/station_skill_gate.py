# CHANGELOG (last 10 broad changes):
# 1. [2026-07-19 Phase 2: Add 850-mb temperature advection signal + wire into engine]
# 2. [2026-07-16 T5-fix: Fix station_skill_gate imports and remove pandas dependency]
# 3. [2026-07-16 T5: Wire per-station skill gating into paper trading engine]
#


import json
import logging
from typing import Dict, List, Optional
import numpy as np
from datetime import datetime, timezone, timedelta
import os
import sqlite3
import sys
from pathlib import Path

# Add repo root to path for imports
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


try:
    from scripts.per_station_skill import (
        compute_rolling_bss,
        brier_skill_score,
        block_bootstrap_ci,
        load_market_directions,
        load_daily_highs_lows,
        brier_score,
    )
except ImportError as e:
    print(f"Warning: Could not import from per_station_skill: {e}")
    # Define stubs for fallback
    def compute_rolling_bss(days, market, station, market_type, window=30):
        return []
    def brier_skill_score(model_preds, model_actuals, baseline_preds, baseline_actuals):
        return 0.0
    def block_bootstrap_ci(data, block_size=5, n_boot=2000, confidence=0.95):
        return (0.0, 0.0)
    def load_market_directions(station, conn, market_type='HIGH'):
        return {}
    def load_daily_highs_lows(station, conn):
        return []


logger = logging.getLogger(__name__)

class StationSkillGate:
    """
    Class to manage per-station skill assessment and gate trading decisions
    based on Brier Skill Score (BSS) thresholds.
    """
    
    # Fixed cache location, decoupled from DB path (I4 fix)
    DEFAULT_CACHE_PATH = "data/bss_cache.json"

    def __init__(self, metar_db_path: str, cache_path: str = None):
        """
        Initialize the skill gate with METAR database path.
        
        Args:
            metar_db_path: Path to the METAR database file
            cache_path: Optional path for BSS cache; defaults to data/bss_cache.json
        """
        self.metar_db_path = metar_db_path
        self._cache_file = Path(cache_path or self.DEFAULT_CACHE_PATH).resolve()
        # Ensure parent directory exists
        self._cache_file.parent.mkdir(parents=True, exist_ok=True)
        self._bss_matrix = None
        self._approved_station_cache = {}
        
        # Load or compute BSS matrix
        self._load_or_compute_bss_matrix()
    
    def _load_or_compute_bss_matrix(self, cache_valid_hours: int = 24):
        """
        Load BSS matrix from cache if available and recent, otherwise compute it.
        
        Args:
            cache_valid_hours: How many hours cache is considered valid
        """
        try:
            # Check if cache exists and is valid
            if (self._cache_file.exists() and 
                datetime.fromtimestamp(self._cache_file.stat().st_mtime) > 
                datetime.now(timezone.utc) - timedelta(hours=cache_valid_hours)):
                
                with open(self._cache_file, 'r') as f:
                    cached_data = json.load(f)
                    self._bss_matrix = cached_data.get('bss_matrix', {})
                    
                logger.info(f"Loaded BSS matrix from cache: {self._cache_file}")
                return
        except Exception as e:
            logger.warning(f"Failed to load BSS cache, will recompute: {e}")
        
        # Compute the BSS matrix
        self._bss_matrix = self._compute_bss_matrix()
        
        # Save to cache
        try:
            with open(self._cache_file, 'w') as f:
                json.dump({
                    'timestamp': datetime.now(timezone.utc).isoformat(),
                    'bss_matrix': self._bss_matrix
                }, f, indent=2)
        except Exception as e:
            logger.warning(f"Failed to save BSS cache: {e}")
    
    def _compute_bss_matrix(self) -> Dict[str, Dict[str, float]]:
        """
        Compute BSS matrix from METAR data for all stations and both market types.
        
        Returns:
            Dict in format {station: {market_type: bss_value}}
        """
        bss_matrix = {}
        
        try:
            # Load database connection
            conn = sqlite3.connect(self.metar_db_path)
            
            # Get all unique stations from the settlement_epochs table
            cursor = conn.cursor()
            cursor.execute("SELECT DISTINCT station FROM settlement_epochs")
            stations = [row[0] for row in cursor.fetchall()]
            conn.close()
            
            if not stations:
                logger.warning("No stations found in settlement_epochs table")
                return {}
                
            market_types = ["HIGH", "LOW"]
            
            logger.info(f"Computing BSS for {len(stations)} stations across {len(market_types)} market types")
            
            for i, station in enumerate(stations):
                bss_matrix[station] = {}
                
                for market_type in market_types:
                    logger.debug(f"Computing BSS for station {station}, market {market_type} ({i+1}/{len(stations)})")
                    
                    try:
                        # Establish new connection for this calculation
                        conn = sqlite3.connect(self.metar_db_path)
                        
                        # Load the high/low data and market directions
                        days = load_daily_highs_lows(station, conn)
                        market_directions = load_market_directions(station, conn, market_type)
                        
                        conn.close()
                        
                        # Calculate the rolling BSS
                        results = compute_rolling_bss(
                            days=days,
                            market=market_directions,
                            station=station,
                            market_type=market_type,
                            window=30  # Using 30 day window
                        )
                        
                        if results:
                            # Use the most recent BSS value
                            # Calculate overall skill by combining persistence and climatology BSS
                            # We require both to be above 0 to consider the station skilled
                            latest_bss = results[-1]  # Most recent
                            bss_persistence = latest_bss.get('bss_persistence', 0.0)
                            bss_climatology = latest_bss.get('bss_climatology', 0.0)
                            
                            # We'll use the minimum BSS vs both baselines as our measure
                            # This ensures the station demonstrates skill against BOTH
                            bss_val = min(bss_persistence, bss_climatology)
                        else:
                            # If no results, return negative skill
                            bss_val = -1.0
                        
                        bss_matrix[station][market_type] = bss_val
                        
                        # Log if the station is skillful
                        if bss_val > 0:
                            logger.debug(f"Station {station} {market_type}: BSS = {bss_val:.3f} (SKILLED)")
                        else:
                            logger.debug(f"Station {station} {market_type}: BSS = {bss_val:.3f}")
                            
                    except Exception as e:
                        logger.warning(f"Could not compute BSS for station {station}, market {market_type}: {e}")
                        bss_matrix[station][market_type] = -1.0  # Default to negative skill
                    
        except Exception as e:
            logger.error(f"Error computing BSS matrix: {e}")
            # Return default empty matrix if failed
            return {}
        
        logger.info(f"BSS matrix computation completed: {len(bss_matrix)} stations")
        return bss_matrix
    
    def is_station_skilled(self, station: str, market_type: str = "HIGH") -> bool:
        """
        Check if a station has demonstrated skill (BSS > 0 vs both baselines).
        
        Args:
            station: Station identifier
            market_type: "HIGH" or "LOW" market type
            
        Returns:
            True if station has demonstrated skill, False otherwise
        """
        # Use cached result if available
        cache_key = f"{station}_{market_type}"
        if cache_key in self._approved_station_cache:
            return self._approved_station_cache[cache_key]
        
        if station not in self._bss_matrix:
            # If station not in matrix, default to trading (conservative: allow unknown stations)
            logger.warning(f"Station {station} not found in BSS matrix, defaulting to allowing trades")
            self._approved_station_cache[cache_key] = True
            return True
        
        if market_type not in self._bss_matrix[station]:
            # If market type not available, default to trading
            logger.warning(f"Market type {market_type} not available for station {station}, defaulting to allowing trades")
            self._approved_station_cache[cache_key] = True
            return True
        
        bss_value = self._bss_matrix[station][market_type]
        is_skilled = bss_value > 0.0  # Only allow if BSS > 0 compared to both baselines
        
        if is_skilled:
            logger.debug(f"Station {station} ({market_type}): ALLOWED (BSS={bss_value:.3f})")
        else:
            logger.debug(f"Station {station} ({market_type}): BLOCKED (BSS={bss_value:.3f})")
        
        # Cache the result
        self._approved_station_cache[cache_key] = is_skilled
        return is_skilled
    
    def get_skilled_stations(self, market_type: str = "HIGH") -> List[str]:
        """
        Get list of stations that meet skill threshold for a given market type.
        
        Args:
            market_type: "HIGH" or "LOW" market type
            
        Returns:
            List of station IDs that demonstrate skill
        """
        skilled = []
        for station, markets_data in self._bss_matrix.items():
            if market_type in markets_data and markets_data[market_type] > 0.0:
                skilled.append(station)
        
        logger.info(f"Found {len(skilled)} skilled stations for {market_type} market")
        return skilled
    
    def get_bss_matrix(self) -> Dict[str, Dict[str, float]]:
        """
        Get the full BSS matrix.
        
        Returns:
            Dict mapping stations to market types to BSS values
        """
        return self._bss_matrix.copy()
    
    def refresh(self):
        """
        Force recomputation of the BSS matrix from scratch.
        """
        # Clear cache
        self._approved_station_cache = {}
        
        # Delete cache file if exists
        if self._cache_file.exists():
            try:
                self._cache_file.unlink()
            except Exception:
                logger.warning(f"Could not delete old cache file: {self._cache_file}")
        
        # Recompute BSS matrix
        self._bss_matrix = self._compute_bss_matrix()
        
        # Save to cache
        try:
            with open(self._cache_file, 'w') as f:
                json.dump({
                    'timestamp': datetime.now(timezone.utc).isoformat(),
                    'bss_matrix': self._bss_matrix
                }, f, indent=2)
        except Exception as e:
            logger.warning(f"Failed to save refreshed BSS cache: {e}")
        
        logger.info("Station skill gate data refreshed successfully")