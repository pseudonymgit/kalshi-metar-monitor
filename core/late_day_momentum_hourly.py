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
import numpy as np
from datetime import datetime, timezone
from typing import Optional, Tuple, Dict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
METAR_DB = str(REPO_ROOT / "data" / "metar_backfill.db")

# ─── Configuration ────────────────────────────────────────────────────────

LATE_DAY_START_HOUR = 17   # UTC hour to start the window (inclusive)
LATE_DAY_END_HOUR   = 22   # UTC hour to end the window (inclusive)
RATE_THRESHOLD      = 1.7  # °F/hour — minimum slope to trigger a signal (optimized 2026-07-04)
MIN_OBS             = 3     # minimum hourly obs needed to compute a slope
MAX_CONFIDENCE      = 0.90  # cap on confidence output


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
    """Linear-regression slope of temp vs. hour."""
    n = len(hours)
    if n < 2:
        return 0.0
    X = np.array(hours, dtype=float)
    Y = np.array(temps, dtype=float)
    sum_x = X.sum()
    sum_y = Y.sum()
    sum_xy = (X * Y).sum()
    sum_x_sq = (X ** 2).sum()
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


def late_day_momentum_hourly(
    station: str,
    date_str: str,
    conn: sqlite3.Connection,
) -> Tuple[Optional[str], float, float]:
    """
    Compute the hourly late-day momentum signal.

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

    slope = _compute_slope(hours, temps)

    if abs(slope) < RATE_THRESHOLD:
        return None, 0.0, 0.5

    direction = "up" if slope > 0 else "down"

    # Confidence: excess over threshold, normalized, boosted by consistency
    excess = abs(slope) - RATE_THRESHOLD
    base_conf = min(1.0, excess / (RATE_THRESHOLD * 2))
    consistency = _consistency_score(hours, temps, slope)
    base_conf *= (0.5 + consistency * 0.5)
    confidence = min(MAX_CONFIDENCE, base_conf)

    # Raw probability for Brier scoring
    raw_prob = 0.50 + min(0.40, abs(slope) / (RATE_THRESHOLD * 4))
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
        print(f"Slope: {slope:.3f} °F/hour")
        print(f"Threshold: {RATE_THRESHOLD} °F/hour")

    print(f"\nSignal: direction={direction}, confidence={conf:.3f}, raw_prob={prob:.3f}")
    conn.close()


if __name__ == "__main__":
    main()
