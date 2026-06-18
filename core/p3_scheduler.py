"""
Phase 3 Scheduler

Event-driven scheduler for Phase 3 prediction layer.

Key features:
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
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

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
    from core.kalshi_monitor import get_discovered_weather_market_station_mapping
    _discovered_stations = get_discovered_weather_market_station_mapping()
    if _discovered_stations:
        # Use discovered stations as the primary list
        ACTIVE_STATIONS = sorted(list(_discovered_stations.keys()))
except Exception:
    pass

# Market types (hourly added for NYC)
MARKET_TYPES = ["high", "low", "hourly"]

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
                timestamp_utc=datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ"),
            )
        
        # Step 2: Get closed epochs (corpus)
        corpus = get_closed_epochs_for_station(station, market_type, limit_days=365)
        if not corpus:
            return PredictionResponse(
                success=False,
                message=f"No historical data found for {station}/{market_type}",
                prediction=None,
                timestamp_utc=datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ"),
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
                timestamp_utc=datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ"),
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
                "timestamp_utc": datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ"),
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
            timestamp_utc=datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ"),
        )


def run_predictions_for_all_stations() -> Dict[str, PredictionResponse]:
    """
    Run predictions for all active stations and market types.
    
    Returns dict of (station, market_type) -> PredictionResponse.
    """
    results = {}
    
    for station in ACTIVE_STATIONS:
        for market_type in MARKET_TYPES:
            key = f"{station}:{market_type}"
            results[key] = run_prediction_for_station(station, market_type)
    
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
    print(f"[{datetime.now().strftime('%Y-%m-%dT%H:%M:%SZ')}] POST-SETTLEMENT HOOK TRIGGERED")
    print("Running Phase 3 predictions for all stations...")
    
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
    Start background worker thread for prediction runs.
    
    The worker runs post-settlement hooks periodically to ensure
    predictions are up-to-date.
    """
    global _worker_thread, _worker_running
    
    if _worker_running:
        return
    
    _worker_running = True
    
    def worker_loop():
        """Background worker loop."""
        while _worker_running:
            try:
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
