"""
Phase 3 API Endpoint

FastAPI endpoints for Phase 3 predictions.

Key features:
- Read-only API endpoints for predictions
- Zero new Kalshi API calls if prediction is purely epoch-history-based
- If current market prices needed: use existing hydration cache, not inline API calls
- Prediction must follow execution-domain gating (no live calls from observability)
- Supports: GET /api/prediction/<station>/<market_type>
"""

from fastapi import APIRouter, HTTPException, Query, Request, Response
from typing import Optional

import core.p3_scheduler as p3sch
import core.p3_output_formatter as p3of
from core.station_time import parse_iso_utc


# FastAPI router
router = APIRouter(
    prefix="/api/prediction",
    tags=["prediction"],
)


@router.get("/{station}/{market_type}")
async def get_prediction(
    request: Request,
    station: str,
    market_type: str = Query(..., description="Market type: 'high' or 'low'"),
    timestamp: Optional[str] = Query(None, description="Query timestamp (ISO UTC)"),
):
    """
    Get prediction for a specific station and market type.
    
    This endpoint is read-only and does NOT call the Kalshi API.
    It retrieves predictions from the Phase 3 prediction layer which:
    - Reads from L4 settlement epochs
    - Never writes to L0-L4
    - Uses deterministic scoring with fixed weights
    
    Args:
        station: Station code (e.g., KDEN, KLAX)
        market_type: Market type ("high" or "low")
        timestamp: Optional query timestamp for historical lookups
        
    Returns:
        Prediction message with PRIMARY/SECONDARY projections,
        confidence band, and any warnings/errors
    """
    # Validate market type
    if market_type not in ["high", "low"]:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid market_type: {market_type}. Must be 'high' or 'low'.",
        )
    
    # Get prediction (from cache or generate fresh)
    prediction = p3sch.get_cached_prediction(station, market_type)
    
    if prediction is None:
        # Generate fresh prediction
        result = p3sch.run_prediction_for_station(station, market_type)
        if not result.success:
            raise HTTPException(
                status_code=404,
                detail=result.message,
            )
        prediction = result.prediction
    
    # Return JSON response
    return {
        "station": prediction.station,
        "market_type": prediction.market_type,
        "epoch_id": prediction.epoch_id,
        "local_trading_date": prediction.local_trading_date,
        "timestamp_utc": prediction.timestamp_utc,
        "primary_projection": prediction.primary_projection,
        "secondary_projection": prediction.secondary_projection,
        "matches": {
            "strong": prediction.strong_match_count,
            "weak": prediction.weak_match_count,
        },
        "top_analog": prediction.top_analog,
        "confidence": {
            "score": prediction.confidence,
            "band": prediction.confidence_band,
        },
        "multimodal_block": prediction.multimodal_block,
        "warnings": prediction.warnings,
        "errors": prediction.errors,
        "raw_output": prediction.raw_output,
    }


@router.get("/{station}/all")
async def get_all_predictions_for_station(
    request: Request,
    station: str,
):
    """
    Get predictions for all market types for a station.
    
    Args:
        station: Station code
        
    Returns:
        Dict of market_type -> prediction
    """
    market_types = ["high", "low"]
    predictions = {}
    
    for market_type in market_types:
        try:
            prediction = p3sch.get_cached_prediction(station, market_type)
            if prediction is None:
                result = p3sch.run_prediction_for_station(station, market_type)
                if not result.success:
                    predictions[market_type] = {
                        "error": result.message,
                        "success": False,
                    }
                    continue
                prediction = result.prediction
            
            predictions[market_type] = {
                "success": True,
                "epoch_id": prediction.epoch_id,
                "local_trading_date": prediction.local_trading_date,
                "primary_projection": prediction.primary_projection,
                "secondary_projection": prediction.secondary_projection,
                "matches": {
                    "strong": prediction.strong_match_count,
                    "weak": prediction.weak_match_count,
                },
                "top_analog": prediction.top_analog,
                "confidence": {
                    "score": prediction.confidence,
                    "band": prediction.confidence_band,
                },
                "warnings": prediction.warnings,
                "errors": prediction.errors,
            }
        except Exception as e:
            predictions[market_type] = {
                "error": str(e),
                "success": False,
            }
    
    return predictions


@router.get("/stations")
async def get_station_list():
    """
    Get list of active stations.
    
    Returns 7 stations: KDEN, KLAX, KNYC, KPHL, KMDW, KMIA, KAUS
    """
    return {
        "stations": p3sch.ACTIVE_STATIONS,
        "market_types": p3sch.MARKET_TYPES,
        "total_combinations": len(p3sch.ACTIVE_STATIONS) * len(p3sch.MARKET_TYPES),
    }


@router.get("/cache/stats")
async def get_cache_stats():
    """
    Get prediction cache statistics.
    
    Returns count of cached predictions and timestamps.
    """
    cache = p3sch._cache
    return {
        "cached_count": len(cache),
        "stations": list(set(
            k.split(":")[0] for k in cache.keys() if ":" in k
        )),
        "market_types": list(set(
            k.split(":")[1] for k in cache.keys() if ":" in k and k.split(":")[1] in ["high", "low"]
        )),
        "last_updated_utc": max(
            (v["timestamp_utc"] for v in cache.values()),
            default="none",
        ),
    }


@router.post("/run")
async def run_predictions_now():
    """
    Trigger immediate prediction run for all stations.
    
    This is primarily for testing/debugging. In production, predictions
    are triggered by the post-settlement hook.
    
    Returns:
        Dict of station:market_type -> prediction result
    """
    results = p3sch.run_predictions_for_all_stations()
    
    return {
        "triggered_at_utc": p3of.datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "total_runs": len(results),
        "successful": sum(1 for r in results.values() if r.success),
        "failed": sum(1 for r in results.values() if not r.success),
        "details": {
            k: {
                "success": v.success,
                "message": v.message,
            }
            for k, v in results.items()
        },
    }


@router.post("/cache/clear")
async def clear_cache_endpoint():
    """
    Clear prediction cache.
    
    Use for testing or when cache needs refresh.
    """
    p3sch.clear_cache()
    return {"status": "cache_cleared", "timestamp_utc": p3of.datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ")}


@router.get("/health")
async def health_check():
    """
    Health check endpoint for the prediction layer.
    
    Returns:
        200 OK if prediction layer is operational
    """
    try:
        # Verify database connection
        import sqlite3
        db_path = p3sch._resolve_db_path()
        conn = sqlite3.connect(db_path, timeout=1)
        conn.execute("SELECT 1")
        conn.close()
        
        return {
            "status": "healthy",
            "database": "connected",
            "db_path": db_path,
            "timestamp_utc": p3of.datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
    except Exception as e:
        raise HTTPException(
            status_code=503,
            detail=f"Health check failed: {str(e)}",
        )


@router.get("/debug/features/{station}/{market_type}")
async def debug_features(
    request: Request,
    station: str,
    market_type: str,
):
    """
    Debug endpoint: Show extracted features from latest epoch.
    
    Returns the 14-dimensional feature vector for analysis.
    """
    import core.p3_feature_extractor as p3fe
    
    query_epoch = p3sch.get_latest_settlement_epoch(station, market_type)
    if not query_epoch:
        raise HTTPException(status_code=404, detail="No open epoch found")
    
    features = p3fe.extract_features_from_epoch(query_epoch)
    
    return {
        "features": {
            "settlement_jump_magnitude": features.settlement_jump_magnitude,
            "reversion_occurred": features.reversion_occurred,
            "max_excursion_above_settlement": features.max_excursion_above_settlement,
            "duration_at_or_above_seconds": features.duration_at_or_above_seconds,
            "duration_strictly_above_seconds": features.duration_strictly_above_seconds,
            "terminal_state_reached": features.terminal_state_reached,
            "transition_count": features.transition_count,
            "settlement_bucket": features.settlement_bucket,
            "reversion_latency_seconds": features.reversion_latency_seconds,
            "goldilocks_emitted": features.goldilocks_emitted,
            "prior_settlement_bucket": features.prior_settlement_bucket,
            "local_trading_date_normalized": features.local_trading_date_normalized,
            "station_gate": features.station,
            "market_type_gate": features.market_type,
        },
        "raw_epoch": {
            "id": query_epoch.get("id"),
            "local_trading_date": query_epoch.get("local_trading_date"),
            "epoch_status": query_epoch.get("epoch_status"),
        },
    }


@router.get("/debug/matches/{station}/{market_type}")
async def debug_matches(
    request: Request,
    station: str,
    market_type: str,
):
    """
    Debug endpoint: Show match analysis.
    
    Returns strong/weak matches and their scores.
    """
    import core.p3_match_engine as p3me
    
    query_epoch = p3sch.get_latest_settlement_epoch(station, market_type)
    if not query_epoch:
        raise HTTPException(status_code=404, detail="No open epoch found")
    
    corpus = p3sch.get_closed_epochs_for_station(station, market_type, limit_days=365)
    if not corpus:
        raise HTTPException(status_code=404, detail="No historical data found")
    
    query_features = p3fe.extract_features_from_epoch(query_epoch)
    match_result = p3me.find_similar_epochs(query_features, corpus)
    
    return {
        "station": station,
        "market_type": market_type,
        "query_epoch_id": query_epoch.get("id"),
        "regime": match_result.regime,
        "strong_matches": [
            {
                "id": m.matched_epoch_id,
                "score": m.match_score,
                "date": m.epoch_data.get("local_trading_date"),
            }
            for m in match_result.strong_matches[:5]
        ],
        "weak_matches": [
            {
                "id": m.matched_epoch_id,
                "score": m.match_score,
                "date": m.epoch_data.get("local_trading_date"),
            }
            for m in match_result.weak_matches[:5]
        ],
        "total_analogs": match_result.total_analogs,
    }
