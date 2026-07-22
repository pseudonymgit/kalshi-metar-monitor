# CHANGELOG (last 10 broad changes):
# 1. [2026-07-05 R4-1.1: Fix P&L mark-to-market + thread-safe price cache]
# 2. [2026-06-27 Phase 3 implementation: dynamic market types, rate limiting, type hints, enhanced health check]
# 3. [2026-06-17 Phase 3: Dynamic Station Discovery + Full 20-City Coverage]
#


"""
Phase 3 Scheduler

Event-driven scheduler for Phase 3 prediction layer.

Key features:
- Daily Kalshi market discovery (calls discover_market_derived_station_codes)
- Post-settlement hook trigger (event-driven, not cron)
- Runs AFTER daily ingestion/settlement pipeline completes
- Trigger on L4 commit for all active stations
- Prediction computation is read-only, can run in any execution domain
- No new Kalshi API calls if prediction is purely epoch-history-based
"""

import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.settlement_epoch_logger import _alert_db_path as get_db_path
import core.p3_feature_extractor as p3fe
import core.p3_match_engine as p3me
import core.p3_trajectory_tracer as p3tt
import core.p3_calibration_engine as p3ce
import core.p3_output_formatter as p3of


# Active stations - populated from Kalshi discovery, with fallback to 7 primary stations
ACTIVE_STATIONS = [
    "KDEN",  # Denver
    "KLAX",  # Los Angeles
    "KNYC",  # New York (Central Park ASOS)
    "KPHL",  # Philadelphia
    "KMDW",  # Chicago (Midway)
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
        This replaces previous static mapping with dynamic discovery.
        """
        try:
            discovered_stations = discover_market_derived_station_codes(max_pages=5, page_limit=200)
            if discovered_stations:
                print(f"[INFO] Discovered {len(discovered_stations)} Kalshi station codes: {discovered_stations[:10]}...{(len(discovered_stations)>10) and '...' or ''}")
                
                # Update global active stations immediately
                global ACTIVE_STATIONS
                ACTIVE_STATIONS = sorted(list(set(discovered_stations)))
                
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
                    
                return discovered_stations
            else:
                print("[WARNING] Kalshi discovery returned empty")
                return []
        except Exception as e:
            print(f"[ERROR] Kalshi discovery failed: {e}")
            return []

    # Refresh at initialization
    _discovered_stations = get_discovered_weather_market_station_mapping()
    if _discovered_stations:
        # Use discovered stations as the primary list
        ACTIVE_STATIONS = sorted(list(_discovered_stations.keys()))
    else:
        # Refresh the discovery mapping if empty
        refresh_kalshi_station_mapping()
        # Try to load again
        _discovered_stations = get_discovered_weather_market_station_mapping()
        if _discovered_stations:
            ACTIVE_STATIONS = sorted(list(_discovered_stations.keys()))
except Exception as e:
    print(f"Kalshi discovery initialization error: {e}")
    pass

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

# Prediction result cache
_cache: Dict[str, Dict[str, Any]] = {}
_cache_lock = threading.Lock()


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
    prediction: Optional[p3of.PredictionMessage]
    timestamp_utc: str


def _resolve_db_path() -> str:
    """Resolve the actual database path."""
    return get_db_path()


def get_latest_settlement_epoch(
    station: str,
    market_type: Optional[str],
) -> Optional[Dict[str, Any]]:
    """
    Get the latest open settlement epoch for a station/market_type.
    
    This is the epoch we want to predict for.
    """
    db_path = _resolve_db_path()
    
    import sqlite3
    conn = sqlite3.connect(db_path, timeout=1)
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT * FROM settlement_epochs
            WHERE station = ?
              AND ((market_type IS NULL AND ? IS NULL) OR market_type = ?)
              AND epoch_status = 'open'
            ORDER BY id DESC
            LIMIT 1
            """,
            (station, market_type, market_type),
        )
        row = cursor.fetchone()
        if row:
            # Get column names
            columns = [desc[0] for desc in cursor.description]
            return dict(zip(columns, row))
    finally:
        conn.close()
    
    return None


def get_closed_epochs_for_station(
    station: str,
    market_type: Optional[str],
    limit_days: int = 365,
) -> List[Dict[str, Any]]:
    """
    Get closed epochs for a station for historical matching.
    
    Looks back up to limit_days from the latest epoch.
    """
    db_path = _resolve_db_path()
    
    import sqlite3
    conn = sqlite3.connect(db_path, timeout=1)
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT * FROM settlement_epochs
            WHERE station = ?
              AND ((market_type IS NULL AND ? IS NULL) OR market_type = ?)
              AND epoch_status = 'closed'
            ORDER BY local_trading_date DESC, id DESC
            LIMIT 500
            """,
            (station, market_type, market_type),
        )
        rows = cursor.fetchall()
        columns = [desc[0] for desc in cursor.description]
        return [dict(zip(columns, row)) for row in rows]
    finally:
        conn.close()


def run_prediction_for_station(
    station: str,
    market_type: Optional[str] = None,
) -> PredictionResponse:
    """
    Run prediction for a single station/market_type combination.
    
    1. Get latest open epoch (query epoch)
    2. Get closed epochs (corpus)
    3. Extract features from query epoch
    4. Find similar epochs in corpus
    5. Trace forward trajectories from analogs
    6. Calculate confidence
    7. Format output
    
    Returns PredictionResponse with success status and message.
    """
    try:
        # Step 1: Get query epoch
        query_epoch = get_latest_settlement_epoch(station, market_type)
        if not query_epoch:
            return PredictionResponse(
                success=False,
                message=f"No open epoch found for {station}/{market_type}",
                prediction=None,
                timestamp_utc=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            )
        
        # Step 2: Get closed epochs (corpus)
        corpus = get_closed_epochs_for_station(station, market_type, limit_days=365)
        if not corpus:
            return PredictionResponse(
                success=False,
                message=f"No historical data found for {station}/{market_type}",
                prediction=None,
                timestamp_utc=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            )
        
        # Step 3: Extract features
        query_features = p3fe.extract_features_from_epoch(query_epoch)
        
        # Step 4: Find analogs
        match_result = p3me.find_similar_epochs(query_features, corpus)
        
        if not match_result.strong_matches and not match_result.weak_matches:
            return PredictionResponse(
                success=False,
                message=f"No compatible analogs found for {station}/{market_type}",
                prediction=None,
                timestamp_utc=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            )
        
        # Step 5: Get top analogs
        top_analogs = p3me.get_top_analogs(match_result, k=10)
        
        # Step 6: Trace trajectories (if we have strong matches)
        trajectory_result = None
        if match_result.strong_matches:
            trajectory_result = p3tt.trace_all_trajectories(
                match_result.strong_matches,
                corpus,
            )
        
        # Step 7: Calculate confidence
        # Use match scores as outcomes for multimodal detection
        match_scores = [m.match_score for m in top_analogs]
        
        # Calculate simple statistics for confidence factors
        n = len(top_analogs)
        if n >= 2:
            mean_score = sum(match_scores) / n
            variance = sum((s - mean_score) ** 2 for s in match_scores) / n
            sigma = variance ** 0.5
            mu = mean_score
        else:
            sigma = 0.0
            mu = 1.0
        
        # Calculate excess kurtosis (simplified)
        if n >= 4:
            mean = sum(match_scores) / n
            m2 = sum((s - mean) ** 2 for s in match_scores) / n
            m4 = sum((s - mean) ** 4 for s in match_scores) / n
            if m2 > 0:
                excess_kurtosis = (m4 / m2 ** 2) - 3
            else:
                excess_kurtosis = 0.0
        else:
            excess_kurtosis = 0.0
        
        # Brier score (placeholder - real implementation would track this)
        brier_score = 0.2
        
        # Temporal proximity (placeholder)
        delta_t_hours = 0.0
        
        # Probability estimates (placeholder)
        p_up = 0.6
        p_down = 0.4
        
        confidence = p3ce.calculate_confidence(
            n=n,
            excess_kurtosis=excess_kurtosis,
            sigma=sigma,
            mu=mu,
            brier_score=brier_score,
            delta_t_hours=delta_t_hours,
            p_up=p_up,
            p_down=p_down,
            outcomes=match_scores,
        )
        
        # Step 8: Format output
        prediction = p3of.create_prediction(
            station=station,
            market_type=market_type,
            epoch_data=query_epoch,
            analogs=top_analogs,
            trajectory_result=trajectory_result,
            confidence=confidence,
        )
        
        # Step 9: Cache result
        cache_key = f"{station}:{market_type}"
        with _cache_lock:
            _cache[cache_key] = {
                "prediction": prediction,
                "timestamp_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
        
        return PredictionResponse(
            success=True,
            message=f"Prediction generated for {station}/{market_type}",
            prediction=prediction,
            timestamp_utc=prediction.timestamp_utc,
        )
        
    except Exception as e:
        return PredictionResponse(
            success=False,
            message=f"Prediction failed for {station}/{market_type}: {str(e)}",
            prediction=None,
            timestamp_utc=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )


def run_predictions_for_all_stations() -> Dict[str, PredictionResponse]:
    """
    Run predictions for all active stations and market types.
    
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
                    message=f"Prediction crashed for {station}/{market_type}: {str(e)}",
                    prediction=None,
                    timestamp_utc=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                )
    
    # If any failures occurred, clear cache to avoid inconsistent state
    if failed_keys:
        clear_cache()
        print(f"[WARNING] {len(failed_keys)} prediction(s) failed, cache cleared to prevent inconsistent state")
    
    return results


def get_cached_prediction(
    station: str,
    market_type: Optional[str] = None,
) -> Optional[p3of.PredictionMessage]:
    """Get cached prediction if available."""
    cache_key = f"{station}:{market_type}"
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
    Runs predictions for all station/market_type combinations.
    """
    print(f"[{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}] POST-SETTLEMENT HOOK TRIGGERED")
    print("Running Phase 3 predictions for all stations...")
    
    results = run_predictions_for_all_stations()
    
    success_count = sum(1 for r in results.values() if r.success)
    fail_count = len(results) - success_count
    
    print(f"[{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}] POST-SETTLEMENT HOOK COMPLETE")
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
    Start background worker thread for prediction runs.
    
    The worker runs daily Kalshi discovery to update station mapping,
    and runs post-settlement hooks periodically to ensure predictions are up-to-date.
    """
    global _worker_thread, _worker_running
    
    if _worker_running:
        return
    
    _worker_running = True
    
    def worker_loop():
        """Background worker loop."""
        last_daily_task = datetime.now(timezone.utc)  # Track when daily task was run
        daily_task_cooldown = timedelta(hours=24)  # Run daily task once per day
        
        while _worker_running:
            try:
                # Run daily Kalshi discovery task approximately once per day
                now = datetime.now(timezone.utc)
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
                
                # Run predictions
                run_predictions_for_all_stations()
                
                # Wait 5 minutes before next run
                time.sleep(300)
            except Exception as e:
                print(f"Worker error: {e}")
                time.sleep(60)
    
    _worker_thread = threading.Thread(target=worker_loop, daemon=True)
    _worker_thread.start()
    print("Phase 3 prediction worker started")
    
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
    print("Phase 3 prediction worker stopped")


# Initialize on module load
start_prediction_worker()