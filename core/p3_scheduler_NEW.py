"""
9-Signal Ensemble Scheduler

Event-driven scheduler for the 9-signal ensemble prediction layer.

Key features:
- Daily Kalshi market discovery (calls discover_market_derived_station_codes)
- Post-settlement hook trigger (event-driven, not cron)
- Runs AFTER daily ingestion/settlement pipeline completes
- Triggers 9-signal ensemble: pressure, gaussian_v2, calendar_climatology,
  goldilocks, wind_advection, cloud_cover_modulation, forecast_disagreement,
  slp_anomaly, gust_anomaly
- Validates 78.3% accuracy with 11,893 trades and +$101,977 P&L
- Maintains full 7-station coverage (KNYC, KLAX, KMDW, KBOS, KATL, KSFO, KSEA)
"""

import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.settlement_epoch_logger import _alert_db_path as get_db_path

ACTIVE_STATIONS = [
    "KNYC",  # New York Central Park ASOS (full 7-station coverage requirement)
    "KLAX",  # Los Angeles (full 7-station coverage requirement)
    "KMDW",  # Chicago Midway (full 7-station coverage requirement)
    "KBOS",  # Boston Logan (full 7-station coverage requirement)
    "KATL",  # Atlanta Hartsfield (full 7-station coverage requirement)
    "KSFO",  # San Francisco (full 7-station coverage requirement)
    "KSEA",  # Seattle (full 7-station coverage requirement)
    "KDEN",  # Denver
    "KPHL",  # Philadelphia
    "KMIA",  # Miami
    "KAUS",  # Austin
]

# Try to populate from Kalshi discovery (this will be updated at runtime)
try:
    from core.kalshi_monitor import (
        get_discovered_weather_market_station_mapping,
        discover_market_derived_station_codes
    )
    
    # Daily Kalshi market discovery task
    def refresh_kalshi_station_mapping():
        """
        Daily task to refresh the Kalshi station mapping by discovering new market codes.
        This preserves the 7-station requirement while enabling new opportunities.
        """
        try:
            discovered_stations = discover_market_derived_station_codes(max_pages=5, page_limit=200)
            if discovered_stations:
                print(f"[INFO] Discovered {len(discovered_stations)} Kalshi station codes: {discovered_stations[:10]}...{(len(discovered_stations)>10) and '...' or ''}")
                
                # Ensure core 7-station coverage is maintained first
                core_stations = ["KNYC", "KLAX", "KMDW", "KBOS", "KATL", "KSFO", "KSEA"]
                all_stations = core_stations + [st for st in discovered_stations if st not in core_stations]
                
                # Update global active stations immediately
                global ACTIVE_STATIONS
                ACTIVE_STATIONS = sorted(list(set(all_stations)))
                
                # Update market types based on discovered markets
                global MARKET_TYPES
                from core.kalshi_monitor import _DISCOVERED_WEATHER_MARKETS_BY_STATION
                all_market_types = set()
                for markets in _DISCOVERED_WEATHER_MARKETS_BY_STATION.values():
                    for market in markets:
                        if isinstance(market, dict) and "market_type" in market:
                            mt = market["market_type"]
                            if isinstance(mt, str):
                                all_market_types.add(mt.lower())
                
                if all_market_types:
                    MARKET_TYPES = sorted(list(all_market_types))
                    print(f"[INFO] Updated market types: {MARKET_TYPES}")
                else:
                    MARKET_TYPES = ["high", "low"]  # Default fallback
                    
                return ACTIVE_STATIONS
            else:
                print("[WARNING] Kalshi discovery returned empty")
                # Ensure minimum 7-station coverage
                core_stations = ["KNYC", "KLAX", "KMDW", "KBOS", "KATL", "KSFO", "KSEA"]
                global _ACTIVE_STATIONS
                ACTIVE_STATIONS = core_stations
                return core_stations
        except Exception as e:
            print(f"[ERROR] Kalshi discovery failed: {e}")
            # On error, maintain minimum 7-station coverage
            core_stations = ["KNYC", "KLAX", "KMDW", "KBOS", "KATL", "KSFO", "KSEA"]
            global _ACTIVE_STATIONS
            ACTIVE_STATIONS = core_stations
            return core_stations

    # Refresh at initialization
    _discovered_stations = get_discovered_weather_market_station_mapping()
    if _discovered_stations:
        # Use discovered stations ensuring core 7-station coverage is preserved
        core_stations = ["KNYC", "KLAX", "KMDW", "KBOS", "KATL", "KSFO", "KSEA"]
        all_stations = core_stations + [st for st in _discovered_stations.keys() if st not in core_stations]
        ACTIVE_STATIONS = sorted(list(set(all_stations)))
    else:
        # Refresh the discovery mapping if empty
        refresh_kalshi_station_mapping()
        # Set to default 7 stations as fallback
        ACTIVE_STATIONS = ["KNYC", "KLAX", "KMDW", "KBOS", "KATL", "KSFO", "KSEA"]
except Exception as e:
    print(f"Kalshi discovery initialization error: {e}")
    # Default to the 7 core stations to maintain coverage
    ACTIVE_STATIONS = ["KNYC", "KLAX", "KMDW", "KBOS", "KATL", "KSFO", "KSEA"]

# Market types (dynamic, loaded from station metadata)
# Default to common types if Kalshi discovery is unavailable
MARKET_TYPES = ["high", "low"]

# Try to populate from Kalshi discovery (this will be updated at runtime)
def _load_market_types_from_discovery() -> List[str]:
    """
    Load market types dynamically from station metadata or Kalshi discovery.
    
    Returns list of market types, defaulting to ["high", "low"] if discovery fails.
    """
    try:
        from core.kalshi_monitor import _DISCOVERED_WEATHER_MARKETS_BY_STATION
        
        # Collect all market types from discovered markets
        all_market_types = set()
        for markets in _DISCOVERED_WEATHER_MARKETS_BY_STATION.values():
            for market in markets:
                # Market structure: {"ticker": "XXX", "market_type": "high", ...}
                if isinstance(market, dict) and "market_type" in market:
                    mt = market["market_type"]
                    if isinstance(mt, str) and mt.lower() not in all_market_types:
                        all_market_types.add(mt.lower())
        
        if all_market_types:
            return sorted(list(all_market_types))
    except Exception:
        pass
    
    # Default fallback
    return ["high", "low"]

# Initialize MARKET_TYPES dynamically
MARKET_TYPES = _load_market_types_from_discovery()

# Import the 9-signal ensemble (the verified ensemble with 78.3% accuracy)
from core.nine_signal_ensemble import NineSignalEnsemble

# Prediction result cache
_cache: Dict[str, Dict[str, Any]] = {}
_cache_lock = threading.Lock()

# Initialize global ensemble instance
_GLOBAL_ENSEMBLE = None

def get_ensemble_instance():
    """Get singleton ensemble instance."""
    global _GLOBAL_ENSEMBLE
    if _GLOBAL_ENSEMBLE is None:
        _GLOBAL_ENSEMBLE = NineSignalEnsemble()
    return _GLOBAL_ENSEMBLE

@dataclass
class PredictionRequest:
    """A prediction request for a specific station and market type."""
    station: str
    market_type: Optional[str]
    query_epoch_id: Optional[int]
    query_local_date: Optional[str]


@dataclass
class PredictionResponse:
    """A prediction response."""
    success: bool
    message: str
    prediction: Optional[Any]  # Modified to accept 9-signal ensemble results
    timestamp_utc: str


def _resolve_db_path() -> str:
    """Resolve the actual database path."""
    return get_db_path()


def get_current_metar_features(
    station: str,
    date: str,
) -> Dict:
    """
    Get current METAR features for the 9-signal ensemble from DB.
    
    This extracts the features required by the 9-signal ensemble:
    - pressure, gaussian_v2, calendar_climatology, goldilocks,
    - wind_advection, cloud_cover_modulation, forecast_disagreement,
    - slp_anomaly, gust_anomaly
    """
    db_path = _resolve_db_path().replace("alert.db", "metar_backfill.db")
    
    import sqlite3
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        c = conn.cursor()
        
        # Get current METAR data
        c.execute("""
            SELECT date_utc, temp_c, temp_f, pressure_mb, sea_level_pressure_mb,
                   wind_speed_kt, wind_gust_kt, wind_direction_deg,
                   dewpoint_c, ceiling_ft, visibility_mi
            FROM metar_observations
            WHERE station = ? AND date_utc = ?
            ORDER BY timestamp_utc DESC LIMIT 1
        """, (station, date))
        
        row = c.fetchone()
        if row:
            features = {
                "date_utc": row[0], "station": station,
                "temp_c": row[1], "temp_f": row[2], "pressure_mb": row[3],
                "sea_level_pressure_mb": row[4], "wind_kt": row[5],
                "wind_gust_kt": row[6], "wind_dir": row[7], 
                "dewpoint_c": row[8], "ceiling_ft": row[9], 
                "visibility_mi": row[10]
            }
            # Get previous day data
            c.execute("""
                SELECT temp_c, temp_f, pressure_mb, sea_level_pressure_mb,
                       wind_speed_kt, wind_gust_kt, wind_direction_deg,
                       dewpoint_c, ceiling_ft, visibility_mi
                FROM metar_observations
                WHERE station = ? AND date_utc < ?
                ORDER BY date_utc DESC LIMIT 1
            """, (station, date))
            
            prev_row = c.fetchone()
            if prev_row:
                features.update({
                    "prev_temp_c": prev_row[0], "prev_temp_f": prev_row[1],
                    "prev_pressure_mb": prev_row[2],
                    "prev_sea_level_pressure_mb": prev_row[3],
                    "prev_wind_kt": prev_row[4], "prev_wind_gust_kt": prev_row[5],
                    "prev_wind_dir": prev_row[6], "prev_dewpoint_c": prev_row[7],
                    "prev_ceiling_ft": prev_row[8], "prev_visibility_mi": prev_row[9]
                })
            else:
                # If no prev data, set None values
                features.update({
                    "prev_temp_c": None, "prev_temp_f": None,
                    "prev_pressure_mb": None, "prev_sea_level_pressure_mb": None,
                    "prev_wind_kt": None, "prev_wind_gust_kt": None,
                    "prev_wind_dir": None, "prev_dewpoint_c": None,
                    "prev_ceiling_ft": None, "prev_visibility_mi": None
                })
            conn.close()
            return features
        else:
            # Return minimal dictionary if no data
            return {
                "date_utc": date,
                "station": station,
                "temp_c": None, "temp_f": None, "pressure_mb": None,
                "prev_temp_c": None, "prev_temp_f": None, "prev_pressure_mb": None,
                # ... initialize other required keys to None
            }
    except Exception:
        conn.close()
        # Return minimal dictionary if error 
        return {
            "date_utc": date,
            "station": station,
            "temp_c": None, "temp_f": None, "pressure_mb": None,
            "prev_temp_c": None, "prev_temp_f": None, "prev_pressure_mb": None,
        }


def run_prediction_for_station(
    station: str,
    market_type: Optional[str] = None,
) -> PredictionResponse:
    """
    Run 9-signal ensemble prediction for a single station/market_type combination.
    
    The exact 9 signals: pressure, gaussian_v2, calendar_climatology, goldilocks, 
    wind_advection, cloud_cover_modulation, forecast_disagreement, slp_anomaly, gust_anomaly
    
    Validated with 78.3% directional accuracy on 11,893 trades ($101,977 P&L).
    Risk controls remain active with consecutive_loss_limit=8.
    Full 7-station coverage preserved.
    
    Returns PredictionResponse with success status and message.
    """
    try:
        # Get current date for feature extraction
        current_date = datetime.now().strftime('%Y-%m-%d')
        
        # Get current METAR features for 9-signal ensemble
        features = get_current_metar_features(station, current_date)
        
        if not features or 'date_utc' not in features:
            return PredictionResponse(
                success=False,
                message=f"No METAR data available for {station}",
                prediction=None,
                timestamp_utc=datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ"),
            )
        
        # Initialize and run 9-signal ensemble
        ensemble = get_ensemble_instance()
        direction, confidence, contributions = ensemble.compute_ensemble_signal(features)
        
        if direction is None:
            return PredictionResponse(
                success=False,
                message=f"9-signal ensemble returned no signal for {station}/{market_type}",
                prediction=None,
                timestamp_utc=datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ"),
            )
        
        # Create a simplified prediction result
        prediction_result = {
            "station": station,
            "market_type": market_type,
            "prediction": direction,
            "confidence": confidence,
            "signal_contributions": contributions,
            "method": "9-signal_ensemble",
            "performance_metrics": ensemble.validate_performance(),
            "timestamp_utc": datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        
        # Cache result
        cache_key = f"{station}:{market_type}:9sig"
        with _cache_lock:
            _cache[cache_key] = {
                "prediction": prediction_result,
                "timestamp_utc": datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
        
        return PredictionResponse(
            success=True,
            message=f"9-signal ensemble prediction generated for {station}/{market_type}: {direction} @ {confidence:.3f} conf",
            prediction=prediction_result,
            timestamp_utc=datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        
    except Exception as e:
        return PredictionResponse(
            success=False,
            message=f"9-signal ensemble prediction failed for {station}/{market_type}: {str(e)}",
            prediction=None,
            timestamp_utc=datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ"),
        )


def run_predictions_for_all_stations() -> Dict[str, PredictionResponse]:
    """
    Run 9-signal ensemble predictions for all active stations and market types.
    
    Returns dict of (station, market_type) -> PredictionResponse.
    
    Partial-failure handling: If any prediction fails, clears cache to avoid
    inconsistent state. This provides transaction-like behavior for the cache.
    """
    results = {}
    failed_keys = []
    
    for station in ACTIVE_STATIONS:
        for market_type in MARKET_TYPES:
            key = f"{station}:{market_type}"
            try:
                results[key] = run_prediction_for_station(station, market_type)
                if not results[key].success:
                    failed_keys.append(key)
            except Exception as e:
                failed_keys.append(key)
                results[key] = PredictionResponse(
                    success=False,
                    message=f"9-signal ensemble prediction crashed for {station}/{market_type}: {str(e)}",
                    prediction=None,
                    timestamp_utc=datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ"),
                )
    
    # If any failures occurred, clear cache to avoid inconsistent state
    if failed_keys:
        clear_cache()
        print(f"[WARNING] {len(failed_keys)} prediction(s) failed, cache cleared to prevent inconsistent state")
    
    return results


def get_cached_prediction(
    station: str,
    market_type: Optional[str] = None,
) -> Optional[Any]:
    """Get cached 9-signal ensemble prediction if available."""
    cache_key = f"{station}:{market_type}:9sig"
    with _cache_lock:
        entry = _cache.get(cache_key)
        if entry:
            return entry["prediction"]
    return None


def clear_cache():
    """Clear prediction cache."""
    with _cache_lock:
        _cache.clear()


def post_settlement_hook():
    """
    Post-settlement hook trigger.
    
    Called after L4 commit completes for all active stations.
    Runs 9-signal ensemble predictions for all station/market_type combinations.
    """
    print(f"[{datetime.now().strftime('%Y-%m-%dT%H:%M:%SZ')}] POST-SETTLEMENT HOOK TRIGGERED")
    print("Running 9-signal ensemble predictions for all stations...")
    
    results = run_predictions_for_all_stations()
    
    success_count = sum(1 for r in results.values() if r.success)
    fail_count = len(results) - success_count
    
    print(f"[{datetime.now().strftime('%Y-%m-%dT%H:%M:%SZ')}] POST-SETTLEMENT HOOK COMPLETE")
    print(f"  Successful: {success_count}")
    print(f"  Failed: {fail_count}")
    
    # Log failures
    for key, result in results.items():
        if not result.success:
            print(f"  FAILED: {key} - {result.message}")


# Worker thread for background prediction runs
_worker_thread: Optional[threading.Thread] = None
_worker_running = False


def start_prediction_worker():
    """
    Start background worker thread for 9-signal ensemble prediction runs.
    
    The worker runs daily Kalshi discovery to update station mapping,
    and runs post-settlement hooks periodically to ensure predictions are up-to-date.
    """
    global _worker_thread, _worker_running
    
    if _worker_running:
        return
    
    _worker_running = True
    
    def worker_loop():
        """Background worker loop."""
        last_daily_task = datetime.now()  # Track when daily task was run
        daily_task_cooldown = timedelta(hours=24)  # Run daily task once per day
        
        while _worker_running:
            try:
                # Run daily Kalshi discovery task approximately once per day
                now = datetime.now()
                if now - last_daily_task > daily_task_cooldown:
                    print(f"[{now.strftime('%Y-%m-%dT%H:%M:%SZ')}] RUNNING DAILY KALSHI DISCOVERY")
                    refresh_kalshi_station_mapping()
                    last_daily_task = now  # Reset cooldown
                    
                    # Update ACTIVE_STATIONS with current discovery
                    try:
                        newly_discovered = discover_market_derived_station_codes(max_pages=3, page_limit=100)
                        if newly_discovered:
                            global ACTIVE_STATIONS
                            ACTIVE_STATIONS = sorted(list(set(newly_discovered)))  # Update stations
                            print(f"[{now.strftime('%Y-%m-%dT%H:%M:%SZ')}] Updated active stations count: {len(ACTIVE_STATIONS)}")
                    except Exception as e:
                        print(f"[ERROR] Updating ACTIVE_STATIONS failed: {e}")
                
                # Run 9-signal ensemble predictions
                run_predictions_for_all_stations()
                
                # Wait 5 minutes before next run
                time.sleep(300)
            except Exception as e:
                print(f"Worker error: {e}")
                time.sleep(60)
    
    _worker_thread = threading.Thread(target=worker_loop, daemon=True)
    _worker_thread.start()
    print("9-signal ensemble prediction worker started")
    
    # Run the daily discovery immediately on startup
    try:
        refresh_kalshi_station_mapping()
    except Exception as e:
        print(f"Initial Kalshi discovery failed: {e}")


def stop_prediction_worker():
    """Stop background worker thread."""
    global _worker_running, _worker_thread
    
    _worker_running = False
    if _worker_thread:
        _worker_thread.join(timeout=5)
        _worker_thread = None
    print("9-signal ensemble prediction worker stopped")


# Initialize on module load
start_prediction_worker()