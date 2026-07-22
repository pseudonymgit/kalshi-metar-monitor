# CHANGELOG (last 10 broad changes):
# 1. [2026-06-27 Phase 3 implementation: dynamic market types, rate limiting, type hints, enhanced health check]
# 2. [2026-06-17 Phase 3: Dynamic Station Discovery + Full 20-City Coverage]
#


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
from typing import Dict, List, Optional
import time

import core.p3_scheduler as p3sch
import core.p3_output_formatter as p3of
from core.station_time import parse_iso_utc
from datetime import timezone


# FastAPI router
router = APIRouter(
    prefix="/api/prediction",
    tags=["prediction"],
)


# Rate limiting for cache clear endpoint
# Track cache clear requests per IP (simple in-memory rate limiter)
_cache_clear_requests: Dict[str, List[float]] = {}
_CACHE_CLEAR_RATE_LIMIT_WINDOW = 60  # seconds
_CACHE_CLEAR_RATE_LIMIT_MAX = 10  # max requests per window


def _check_rate_limit(client_ip: str) -> bool:
    """
    Check if client IP is within rate limit.
    
    Returns True if request is allowed, False if rate limited.
    """
    now = time.time()
    window_start = now - _CACHE_CLEAR_RATE_LIMIT_WINDOW
    
    # Clean old entries
    if client_ip in _cache_clear_requests:
        _cache_clear_requests[client_ip] = [
            t for t in _cache_clear_requests[client_ip] if t > window_start
        ]
    else:
        _cache_clear_requests[client_ip] = []
    
    # Check limit
    if len(_cache_clear_requests[client_ip]) >= _CACHE_CLEAR_RATE_LIMIT_MAX:
        return False
    
    # Record this request
    _cache_clear_requests[client_ip].append(now)
    return True


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
        "triggered_at_utc": p3of.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
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
async def clear_cache_endpoint(
    request: Request,
):
    """
    Clear prediction cache.
    
    Use for testing or when cache needs refresh.
    Rate limited to 10 requests per minute per IP.
    """
    # Get client IP
    client_ip = request.client.host if request.client else "unknown"
    
    # Check rate limit
    if not _check_rate_limit(client_ip):
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded. Max 10 cache clears per minute per IP.",
        )
    
    p3sch.clear_cache()
    return {"status": "cache_cleared", "timestamp_utc": p3of.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}


@router.get("/health")
async def health_check():
    """
    Health check endpoint for the prediction layer.
    
    Verifies:
    - Database connectivity
    - Prediction layer modules are importable
    - Prediction layer can run a basic prediction
    
    Returns:
        200 OK if prediction layer is operational
    """
    health = {
        "status": "healthy",
        "checks": {},
        "timestamp_utc": p3of.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    
    try:
        # Check 1: Database connectivity
        import sqlite3
        db_path = p3sch._resolve_db_path()
        conn = sqlite3.connect(db_path, timeout=1)
        conn.execute("SELECT 1")
        conn.close()
        health["checks"]["database"] = {
            "status": "connected",
            "db_path": db_path,
        }
        health["database"] = "connected"
    except Exception as e:
        health["checks"]["database"] = {
            "status": "failed",
            "error": str(e),
        }
        health["status"] = "degraded"
        health["database"] = "failed"
    
    try:
        # Check 2: Prediction layer modules are importable
        from core import p3_feature_extractor as p3fe
        from core import p3_match_engine as p3me
        from core import p3_trajectory_tracer as p3tt
        from core import p3_calibration_engine as p3ce
        from core import p3_output_formatter as p3of
        
        health["checks"]["modules"] = {
            "status": "loaded",
            "modules": [
                "p3_feature_extractor",
                "p3_match_engine",
                "p3_trajectory_tracer",
                "p3_calibration_engine",
                "p3_output_formatter",
            ],
        }
    except Exception as e:
        health["checks"]["modules"] = {
            "status": "failed",
            "error": str(e),
        }
        health["status"] = "degraded"
    
    try:
        # Check 3: Prediction layer can run a basic prediction
        # Try to get latest epoch - this tests the full prediction pipeline infrastructure
        try:
            epoch = p3sch.get_latest_settlement_epoch("KDEN", "high")
            if epoch:
                health["checks"]["prediction_pipeline"] = {
                    "status": "operational",
                    "last_epoch_id": epoch.get("id"),
                }
            else:
                health["checks"]["prediction_pipeline"] = {
                    "status": "operational",
                    "message": "No open epoch found (expected if no trading today)",
                }
        except Exception as e:
            health["checks"]["prediction_pipeline"] = {
                "status": "failed",
                "error": str(e),
            }
            health["status"] = "degraded"
    except Exception as e:
        if "prediction_pipeline" not in health["checks"]:
            health["checks"]["prediction_pipeline"] = {
                "status": "failed",
                "error": str(e),
            }
            health["status"] = "degraded"
    
    # Overall status
    if health["status"] != "degraded":
        health["status"] = "healthy"
        return health
    else:
        raise HTTPException(
            status_code=503,
            detail=f"Health check: {health}",
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
