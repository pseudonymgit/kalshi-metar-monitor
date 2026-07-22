# CHANGELOG (last 10 broad changes):
# 1. [2026-06-17 Phase 3: Dynamic Station Discovery + Full 20-City Coverage]
#


"""
Phase 3 Output Formatter

Formats prediction results into structured messages per Phase 3 spec.

Output includes:
- PRIMARY/SECONDARY projections
- MATCHES counts (strong, weak)
- TOP ANALOG
- CONFIDENCE band
- MULTIMODAL/BLOCK if applicable
- WARNINGS if failure modes triggered
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from core.p3_match_engine import Match
from core.p3_trajectory_tracer import Trajectory, TrajectoryResult, TrajectoryCluster
from core.p3_calibration_engine import ConfidenceScore


# Warning codes (fixed, never learned)
WARNING_CODES = {
    "SPARSE_DATA": "Sparse data regime - fewer than 5 strong matches",
    "WEAK_MATCHES": "Only weak matches available - consider with caution",
    "LOW_CONFIDENCE": "Confidence below actionable threshold (0.60)",
    "NO_CONSENSUS": "No consensus found among analogs",
    "DIVIDED_TRAILS": "Conflicting forward trajectories detected",
    "GAP_IN_TRAJECTORY": "1-epoch gap in trajectory - reduced confidence",
    "TERMINAL_STATE": "Terminal state already reached",
    "STATION_YOUNG": "Station has less than 90 days of history",
    "HYDRATION_BLOCKED": "Market hydration cache invalid or missing",
    "OBSERVATION_LAG": "Observation older than 5 minutes",
    "REGIME_CHANGE": "Unprecedented conditions detected",
}

# Error codes
ERROR_CODES = {
    "NO_ANALOGS": "No compatible analogs found",
    "DB_ERROR": "Database query failed",
    "INSUFFICIENT_DATA": "Insufficient data for prediction",
}


@dataclass(frozen=True)
class PredictionMessage:
    """Structured prediction message per spec."""
    station: str
    market_type: Optional[str]
    epoch_id: int
    local_trading_date: str
    primary_projection: Optional[str]
    secondary_projection: Optional[str]
    strong_match_count: int
    weak_match_count: int
    top_analog: Optional[str]
    confidence: float
    confidence_band: str
    multimodal_block: Optional[str]
    warnings: List[str]
    errors: List[str]
    raw_output: str
    timestamp_utc: str


def format_prediction(
    station: str,
    market_type: Optional[str],
    epoch_id: int,
    local_trading_date: str,
    primary_analog: Optional[Match],
    secondary_analog: Optional[Match],
    strong_matches: List[Match],
    weak_matches: List[Match],
    trajectory_result: Optional[TrajectoryResult],
    confidence: ConfidenceScore,
) -> PredictionMessage:
    """
    Format full prediction message.
    
    Args:
        station: Station code (e.g., "KDEN")
        market_type: Market type (e.g., "high" or "low")
        epoch_id: Query epoch ID
        local_trading_date: Trading date
        primary_analog: Top matched analog
        secondary_analog: Secondary matched analog (if any)
        strong_matches: All strong matches (≥ 0.85)
        weak_matches: All weak matches (0.70-0.85)
        trajectory_result: Trajectory tracing result
        confidence: Confidence score object
        
    Returns:
        PredictionMessage with all required fields
    """
    warnings = []
    errors = []
    
    # Generate warnings
    if len(strong_matches) < 5:
        warnings.append(WARNING_CODES["SPARSE_DATA"])
    
    if not strong_matches and weak_matches:
        warnings.append(WARNING_CODES["WEAK_MATCHES"])
    
    if confidence.final_score < 0.60:
        warnings.append(WARNING_CODES["LOW_CONFIDENCE"])
    
    if not strong_matches and not weak_matches:
        errors.append(WARNING_CODES["NO_CONSENSUS"])
    
    if trajectory_result and trajectory_result.has_divided_consensus:
        warnings.append(WARNING_CODES["DIVIDED_TRAILS"])
    
    if trajectory_result:
        for traj in trajectory_result.successful_trajectories:
            if traj.has_gap:
                warnings.append(WARNING_CODES["GAP_IN_TRAJECTORY"])
                break
    
    if confidence.multimodal_state != "unimodal":
        warnings.append(f"Multimodal distribution: {confidence.multimodal_state}")
    
    # Determine primary and secondary projections
    primary_projection = None
    secondary_projection = None
    
    if primary_analog:
        bucket = primary_analog.epoch_data.get("settlement_bucket", "?")
        primary_projection = f"settlement_bucket={bucket} (weight=100%, N={len(strong_matches) + len(weak_matches)})"
    
    if secondary_analog and secondary_analog.match_score > 0:
        bucket = secondary_analog.epoch_data.get("settlement_bucket", "?")
        # Calculate relative weight
        total_score = sum(m.match_score for m in (strong_matches + weak_matches))
        if total_score > 0:
            rel_weight = int(secondary_analog.match_score / total_score * 100)
        else:
            rel_weight = 0
        secondary_projection = f"settlement_bucket={bucket} (weight={rel_weight}%, N={len(strong_matches) + len(weak_matches)})"
    
    # Format top analog
    top_analog = None
    if primary_analog:
        date = primary_analog.epoch_data.get("local_trading_date", "?")
        top_analog = f"{date} (score={primary_analog.match_score:.3f})"
    
    # Format multimodal block if applicable
    multimodal_block = None
    if confidence.multimodal_state != "unimodal":
        multimodal_block = f"MULTIMODAL BLOCK — {confidence.multimodal_state} distribution detected"
    
    # Generate raw output string
    raw_output = format_raw_output(
        station=station,
        market_type=market_type,
        epoch_id=epoch_id,
        local_trading_date=local_trading_date,
        primary_projection=primary_projection,
        secondary_projection=secondary_projection,
        strong_matches=strong_matches,
        weak_matches=weak_matches,
        top_analog=top_analog,
        confidence=confidence,
        warnings=warnings,
        errors=errors,
    )
    
    return PredictionMessage(
        station=station,
        market_type=market_type,
        epoch_id=epoch_id,
        local_trading_date=local_trading_date,
        primary_projection=primary_projection,
        secondary_projection=secondary_projection,
        strong_match_count=len(strong_matches),
        weak_match_count=len(weak_matches),
        top_analog=top_analog,
        confidence=confidence.final_score,
        confidence_band=confidence.band,
        multimodal_block=multimodal_block,
        warnings=warnings,
        errors=errors,
        raw_output=raw_output,
        timestamp_utc=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )


def format_raw_output(
    station: str,
    market_type: Optional[str],
    epoch_id: int,
    local_trading_date: str,
    primary_projection: Optional[str],
    secondary_projection: Optional[str],
    strong_matches: List[Match],
    weak_matches: List[Match],
    top_analog: Optional[str],
    confidence: ConfidenceScore,
    warnings: List[str],
    errors: List[str],
) -> str:
    """Format raw output string for logging/monitoring."""
    lines = []
    
    # Header
    lines.append(f"PREDICTION: {station} {market_type or 'ALL'} epoch {epoch_id}")
    
    # Projections
    if primary_projection:
        lines.append(f"PRIMARY: {primary_projection}")
    if secondary_projection:
        lines.append(f"SECONDARY: {secondary_projection}")
    
    # Match counts
    lines.append(f"MATCHES: {len(strong_matches)} strong, {len(weak_matches)} weak")
    
    # Top analog
    if top_analog:
        lines.append(f"TOP ANALOG: {top_analog}")
    
    # Confidence
    lines.append(f"CONFIDENCE: {confidence.final_score:.3f} ({confidence.band})")
    
    # Multimodal
    if confidence.multimodal_block:
        lines.append(f"MULTIMODAL: {confidence.multimodal_state}")
    
    # Warnings
    if warnings:
        lines.append("WARNINGS:")
        for w in warnings:
            lines.append(f"  - {w}")
    
    # Errors
    if errors:
        lines.append("ERRORS:")
        for e in errors:
            lines.append(f"  - {e}")
    
    return "\n".join(lines)


def format_json_output(prediction: PredictionMessage) -> Dict[str, Any]:
    """Format prediction as JSON-compatible dictionary."""
    output = {
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
        "warnings": prediction.warnings,
        "errors": prediction.errors,
    }
    
    if prediction.multimodal_block:
        output["multimodal_block"] = prediction.multimodal_block
    
    return output


def format_html_output(prediction: PredictionMessage) -> str:
    """Format prediction as HTML snippet for dashboard."""
    html = f"""
<div class="prediction-block">
    <h3>{prediction.station} {prediction.market_type or "ALL"}</h3>
    <p><strong>Epoch:</strong> {prediction.epoch_id} ({prediction.local_trading_date})</p>
    <p><strong>PRIMARY:</strong> {prediction.primary_projection or "N/A"}</p>
"""
    if prediction.secondary_projection:
        html += f"    <p><strong>SECONDARY:</strong> {prediction.secondary_projection}</p>\n"
    
    html += f"""
    <p><strong>Matches:</strong> {prediction.strong_match_count} strong, {prediction.weak_match_count} weak</p>
    <p><strong>Top Analog:</strong> {prediction.top_analog or "N/A"}</p>
    <p><strong>Confidence:</strong> {prediction.confidence:.3f} <em>({prediction.confidence_band})</em></p>
"""
    
    if prediction.multimodal_block:
        html += f"    <p><strong>Multimodal:</strong> {prediction.multimodal_block}</p>\n"
    
    if prediction.warnings:
        html += "    <p><strong>Warnings:</strong> " + ", ".join(prediction.warnings) + "</p>\n"
    
    html += "    <p><small>Generated at " + prediction.timestamp_utc + "</small></p>\n"
    html += "</div>\n"
    
    return html


def format_csv_header() -> str:
    """Return CSV header row."""
    return ",".join([
        "station",
        "market_type",
        "epoch_id",
        "local_trading_date",
        "timestamp_utc",
        "primary_projection",
        "secondary_projection",
        "strong_matches",
        "weak_matches",
        "top_analog",
        "confidence_score",
        "confidence_band",
        "multimodal_state",
        "warnings",
        "errors",
    ])


def format_csv_row(prediction: PredictionMessage) -> str:
    """Format prediction as CSV row."""
    import csv
    import io
    
    # Escape warnings and errors for CSV
    warnings_str = "; ".join(prediction.warnings).replace('"', '""')
    errors_str = "; ".join(prediction.errors).replace('"', '""')
    
    fields = [
        prediction.station,
        prediction.market_type or "",
        str(prediction.epoch_id),
        prediction.local_trading_date,
        prediction.timestamp_utc,
        prediction.primary_projection or "",
        prediction.secondary_projection or "",
        str(prediction.strong_match_count),
        str(prediction.weak_match_count),
        prediction.top_analog or "",
        str(prediction.confidence),
        prediction.confidence_band,
        prediction.multimodal_block or "",
        f'"{warnings_str}"',
        f'"{errors_str}"',
    ]
    
    # Use csv module for proper escaping
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(fields)
    return output.getvalue().strip()


def create_prediction(
    station: str,
    market_type: Optional[str],
    epoch_data: Dict[str, Any],
    analogs: List[Match],
    trajectory_result: Optional[TrajectoryResult],
    confidence: ConfidenceScore,
) -> PredictionMessage:
    """
    Create a complete prediction message from components.
    
    This is a convenience wrapper that calls format_prediction with
    the right components extracted from the inputs.
    """
    # Split analogs into strong/weak
    strong = [a for a in analogs if a.match_score >= 0.85]
    weak = [a for a in analogs if 0.70 <= a.match_score < 0.85]
    
    # Get top two analogs
    top = analogs[0] if analogs else None
    second = analogs[1] if len(analogs) > 1 else None
    
    return format_prediction(
        station=station,
        market_type=market_type,
        epoch_id=epoch_data.get("id", 0),
        local_trading_date=epoch_data.get("local_trading_date", "unknown"),
        primary_analog=top,
        secondary_analog=second,
        strong_matches=strong,
        weak_matches=weak,
        trajectory_result=trajectory_result,
        confidence=confidence,
    )


def format_error_prediction(
    station: str,
    market_type: Optional[str],
    epoch_id: int,
    error_code: str,
) -> PredictionMessage:
    """Create a prediction with only error (no forecast possible)."""
    error_msg = ERROR_CODES.get(error_code, "Unknown error")
    
    return PredictionMessage(
        station=station,
        market_type=market_type,
        epoch_id=epoch_id,
        local_trading_date="N/A",
        primary_projection=None,
        secondary_projection=None,
        strong_match_count=0,
        weak_match_count=0,
        top_analog=None,
        confidence=0.0,
        confidence_band="INSUFFICIENT",
        multimodal_block=None,
        warnings=[error_msg],
        errors=[error_msg],
        raw_output=f"ERROR: {error_msg}",
        timestamp_utc=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
