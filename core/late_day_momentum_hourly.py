#!/usr/bin/env python3
"""
Hourly Late-Day Momentum Signal Module (v1.0 — 2026-07-04)

Replaces the daily-aggregate proxy in split_backtest.py with a proper
hourly METAR signal that uses the 17:00–23:00 UTC window.

Signal logic:
  1. Query metar_observations for the station/date in hours 17–22 UTC.
  2. Compute linear-regression slope of temp_f vs. hour.
  3. If |slope| ≥ RATE_THRESHOLD, bet WITH the momentum direction.
  4. Confidence scales with excess slope and trend consistency.

This is a standalone module imported by split_backtest_hourly.py.
"""

import sqlite3
import math
from datetime import datetime, timezone
from typing import Optional, Tuple, Dict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
METAR_DB = str(REPO_ROOT / "data" / "metar_backfill.db")

# ─── Configuration ────────────────────────────────────────────────────────

LATE_DAY_START_HOUR = 17   # UTC hour to start the window (inclusive)
LATE_DAY_END_HOUR   = 22   # UTC hour to end the window (inclusive)
BASE_RATE_THRESHOLD = 1.7  # °F/hour — baseline threshold (regime-adaptive, scales with DTR)
MIN_OBS             = 3     # minimum hourly obs needed to compute a slope
MAX_CONFIDENCE      = 0.90  # cap on confidence output
MIN_PERSISTENCE     = 4     # minimum consecutive segments agreeing with slope sign (out of 5)

# DTR regime thresholds (°F)
HIGH_DTR_THRESHOLD    = 15.0   # days with DTR > 15°F → scale threshold up
LOW_DTR_THRESHOLD     = 8.0    # days with DTR < 8°F → scale threshold down
MODERATE_DTR_MIN      = 8.0    # moderate DTR range lower bound
MODERATE_DTR_MAX      = 15.0   # moderate DTR range upper bound


def _load_hourly_obs(station: str, date_str: str, conn: sqlite3.Connection):
    """Fetch hourly METAR temps in the 17–22 UTC window for a station/date.

    Returns a list of (hour_int, temp_f) tuples sorted by hour.
    """
    c = conn.cursor()
    c.execute("""
        SELECT CAST(strftime('%H', timestamp_utc) AS INTEGER) AS hour,
               temp_f
        FROM metar_observations
        WHERE station = ?
          AND date_utc = ?
          AND temp_f IS NOT NULL
          AND CAST(strftime('%H', timestamp_utc) AS INTEGER) >= ?
          AND CAST(strftime('%H', timestamp_utc) AS INTEGER) <= ?
        ORDER BY timestamp_utc
    """, (station, date_str, LATE_DAY_START_HOUR, LATE_DAY_END_HOUR))

    rows = c.fetchall()
    # Deduplicate to one observation per hour (keep the first)
    seen_hours = {}
    for hour, temp in rows:
        if hour not in seen_hours:
            seen_hours[hour] = temp

    return sorted(seen_hours.items())


def _compute_slope(hours: list, temps: list) -> float:
    """Linear-regression slope of temp vs. hour.
    
    Pure Python implementation - no numpy required.
    Uses the formula: slope = (n*sum_xy - sum_x*sum_y) / (n*sum_x_sq - sum_x^2)
    """
    n = len(hours)
    if n < 2:
        return 0.0
    
    # Compute sums using pure Python
    sum_x = 0.0
    sum_y = 0.0
    sum_xy = 0.0
    sum_x_sq = 0.0
    
    for h, t in zip(hours, temps):
        x = float(h)
        y = float(t)
        sum_x += x
        sum_y += y
        sum_xy += x * y
        sum_x_sq += x * x
    
    denom = n * sum_x_sq - sum_x ** 2
    if denom == 0:
        return 0.0
    return (n * sum_xy - sum_x * sum_y) / denom


def _consistency_score(hours: list, temps: list, slope: float) -> float:
    """Fraction of consecutive segments that agree with the overall slope sign."""
    if len(temps) < 3:
        return 0.5
    agreeing = 0
    total = 0
    for i in range(len(temps) - 1):
        dh = hours[i + 1] - hours[i]
        if dh == 0:
            continue
        seg_slope = (temps[i + 1] - temps[i]) / dh
        if abs(seg_slope) > 0.1:  # ignore near-flat segments
            total += 1
            if (seg_slope > 0) == (slope > 0):
                agreeing += 1
    return agreeing / total if total > 0 else 0.5


def _persistence_filter(hours: list, temps: list, slope: float) -> bool:
    """Check if the slope is persistent — at least MIN_PERSISTENCE consecutive
    segments agree with the overall slope sign.
    
    This filters out single-hour spikes that dominate a fragile regression.
    A 5-hour window has at most 4 segments; we require at least MIN_PERSISTENCE=4
    to pass. If there are fewer segments, require all of them to agree.
    """
    if len(temps) < 3:
        return False
    
    agreeing = 0
    total = 0
    for i in range(len(temps) - 1):
        dh = hours[i + 1] - hours[i]
        if dh == 0:
            continue
        seg_slope = (temps[i + 1] - temps[i]) / dh
        if abs(seg_slope) > 0.1:  # ignore near-flat segments
            total += 1
            if (seg_slope > 0) == (slope > 0):
                agreeing += 1
    
    # Require at least MIN_PERSISTENCE agreeing segments, or all segments if fewer
    required = min(MIN_PERSISTENCE, total)
    return agreeing >= required


def _compute_dtr(station: str, date_str: str, conn: sqlite3.Connection) -> Optional[float]:
    """Compute the Diurnal Temperature Range (DTR) from METAR observations
    before the late-day signal window (before 17:00 UTC).
    
    DTR = max(temp_f) - min(temp_f) for observations from 00:00 to 16:00 UTC.
    Returns None if insufficient data.
    """
    c = conn.cursor()
    c.execute("""
        SELECT MAX(temp_f), MIN(temp_f)
        FROM metar_observations
        WHERE station = ?
          AND date_utc = ?
          AND temp_f IS NOT NULL
          AND CAST(strftime('%H', timestamp_utc) AS INTEGER) < ?
    """, (station, date_str, LATE_DAY_START_HOUR))
    
    row = c.fetchone()
    if row and row[0] is not None and row[1] is not None:
        return row[0] - row[1]
    return None


def _regime_adaptive_threshold(dtr: Optional[float]) -> Tuple[float, float]:
    """Compute the regime-adaptive threshold based on DTR.
    
    Returns (threshold, confidence_weight) where:
    - High DTR (>15°F): threshold scales up, confidence weight = 1.0
    - Moderate DTR (8-15°F): baseline threshold, confidence weight = 1.0
    - Low DTR (<8°F): threshold scales down, confidence weight = 0.6
      (low-DTR signals carry less directional certainty)
    
    This replaces the fixed 1.7°F/hr threshold with a regime-aware one.
    """
    if dtr is None:
        # No DTR data — use baseline
        return BASE_RATE_THRESHOLD, 1.0
    
    if dtr > HIGH_DTR_THRESHOLD:
        # Large DTR: scale threshold up (1.7 * DTR/15)
        # e.g., DTR=24 → threshold=2.72°F/hr
        threshold = BASE_RATE_THRESHOLD * (dtr / HIGH_DTR_THRESHOLD)
        return threshold, 1.0
    elif dtr < LOW_DTR_THRESHOLD:
        # Small DTR: scale threshold down (1.7 * DTR/8 * 0.6)
        # e.g., DTR=5 → threshold=0.64°F/hr
        threshold = BASE_RATE_THRESHOLD * (dtr / LOW_DTR_THRESHOLD) * 0.6
        return threshold, 0.6  # Reduced confidence weight for low-DTR signals
    else:
        # Moderate DTR: baseline
        return BASE_RATE_THRESHOLD, 1.0


def late_day_momentum_hourly(
    station: str,
    date_str: str,
    conn: sqlite3.Connection,
) -> Tuple[Optional[str], float, float]:
    """
    Compute the hourly late-day momentum signal.
    
    R4-1.4: Uses regime-adaptive threshold based on DTR (Diurnal Temperature Range)
    and a persistence filter to reduce false positives.

    Args:
        station:  ICAO code (e.g. 'KATL')
        date_str: UTC date in YYYY-MM-DD format
        conn:     SQLite connection to metar_backfill.db

    Returns:
        (direction, confidence, raw_prob) or (None, 0.0, 0.5)
    """
    obs = _load_hourly_obs(station, date_str, conn)
    if len(obs) < MIN_OBS:
        return None, 0.0, 0.5

    hours = [h for h, _ in obs]
    temps = [t for _, t in obs]

    # R4-1.4: Compute DTR and get regime-adaptive threshold
    dtr = _compute_dtr(station, date_str, conn)
    rate_threshold, dtr_confidence_weight = _regime_adaptive_threshold(dtr)

    slope = _compute_slope(hours, temps)

    if abs(slope) < rate_threshold:
        return None, 0.0, 0.5

    # R4-1.4: Persistence filter — require consistent slope across hourly intervals
    if not _persistence_filter(hours, temps, slope):
        return None, 0.0, 0.5

    direction = "up" if slope > 0 else "down"

    # Confidence: excess over threshold, normalized, boosted by consistency
    # R4-1.4: Apply DTR confidence weight for low-DTR signals
    excess = abs(slope) - rate_threshold
    base_conf = min(1.0, excess / (rate_threshold * 2))
    consistency = _consistency_score(hours, temps, slope)
    base_conf *= (0.5 + consistency * 0.5)
    base_conf *= dtr_confidence_weight  # Reduce confidence for low-DTR regime
    confidence = min(MAX_CONFIDENCE, base_conf)

    # Raw probability for Brier scoring
    raw_prob = 0.50 + min(0.40, abs(slope) / (rate_threshold * 4))
    if direction == "down":
        raw_prob = 1.0 - raw_prob

    return direction, confidence, raw_prob


# ─── Standalone Test / CLI ────────────────────────────────────────────────

def main():
    """Quick CLI test of the signal on sample dates."""
    import sys
    station = sys.argv[1] if len(sys.argv) > 1 else "KATL"
    date = sys.argv[2] if len(sys.argv) > 2 else "2024-06-15"

    conn = sqlite3.connect(METAR_DB, timeout=10)
    direction, conf, prob = late_day_momentum_hourly(station, date, conn)

    # Show the underlying data
    obs = _load_hourly_obs(station, date, conn)
    print(f"Station: {station}  Date: {date}")
    print(f"Late-day obs ({LATE_DAY_START_HOUR}-{LATE_DAY_END_HOUR} UTC): {len(obs)}")
    for h, t in obs:
        print(f"  {h:02d}:00 -> {t:.1f}°F")

    if obs and len(obs) >= 2:
        slope = _compute_slope([h for h, _ in obs], [t for _, t in obs])
        dtr = _compute_dtr(station, date, conn)
        threshold, conf_weight = _regime_adaptive_threshold(dtr)
        print(f"Slope: {slope:.3f} °F/hour")
        print(f"DTR: {dtr:.1f}°F" if dtr is not None else "DTR: N/A")
        print(f"Regime threshold: {threshold:.3f} °F/hour (weight: {conf_weight:.1f})")
        persistence = _persistence_filter([h for h, _ in obs], [t for _, t in obs], slope)
        print(f"Persistence filter: {'PASS' if persistence else 'FAIL'}")

    print(f"\nSignal: direction={direction}, confidence={conf:.3f}, raw_prob={prob:.3f}")
    conn.close()


if __name__ == "__main__":
    main()
