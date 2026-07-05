"""
PRODUCTION SIGNAL: 2-Day Cumulative Pressure Tendency
Deployable signal for Kalshi daily HIGH temperature markets.

Uses 2-day cumulative barometric pressure change as directional predictor.
Signal: if (P_yesterday - P_3_days_ago) > threshold, predict UP (warming)
        if (P_yesterday - P_3_days_ago) < -threshold, predict DOWN (cooling)
        otherwise: no signal (skip trade)

Best thresholds:
  ΔP > 2.0mb: 60.35% accuracy, 70.0% coverage — highest coverage that passes 58%
  ΔP > 3.0mb: 61.86% accuracy, 57.4% coverage — best balance
  ΔP > 5.0mb: 64.10% accuracy, 38.7% coverage — highest accuracy

RECOMMENDED: ΔP > 3.0mb (61.86% accuracy, 57.4% coverage)

Seasonal performance (ΔP > 3.0mb):
  Winter: 62.33% (7,487 days)
  Spring: 62.87% (5,901 days)
  Summer: 59.52% (3,619 days)
  Fall:   61.45% (2,477 days)

Best stations: KPHX 68.7%, KLAS 68.7%, KDEN 68.6%
Worst stations: KMIA 56.6%, KLAX 56.1%, KHOU 57.0%
"""

import sqlite3
from typing import Optional, Tuple

METAR_DB = "data/metar_backfill.db"

# Station mapping (corrected)
KALSHI_STATION_MAP = {
    "ATL": "KATL", "AUS": "KAUS", "BOS": "KBOS", "CHI": "KMDW",
    "DAL": "KDFW", "DC":  "KDCA", "DEN": "KDEN", "HOU": "KHOU",
    "LAX": "KLAX", "LV":  "KLAS", "MIA": "KMIA", "MIN": "KMSP",
    "NOLA":"KMSY", "NYC": "KNYC", "OKC": "KOKC", "PHIL":"KPHL",
    "PHX": "KPHX", "SATX":"KSAT", "SEA": "KSEA", "SFO": "KSFO",
}

PRESSURE_THRESHOLD = 3.0  # mb — recommended production threshold


def get_recent_pressure(station: str, days: int = 4) -> list:
    """Get recent daily average pressures for a station."""
    conn = sqlite3.connect(METAR_DB, timeout=60)
    cur = conn.cursor()
    cur.execute("""
        SELECT date_utc, AVG(pressure_mb) as pressure
        FROM metar_observations
        WHERE station=? AND pressure_mb IS NOT NULL AND temp_f IS NOT NULL
        GROUP BY date_utc
        ORDER BY date_utc DESC
        LIMIT ?
    """, (station, days))
    rows = cur.fetchall()
    conn.close()
    return rows


def get_signal(kalshi_city: str) -> Optional[Tuple[str, float]]:
    """
    Get directional signal for a Kalshi city.
    Returns (direction, delta_p) or None if no signal.
    """
    station = KALSHI_STATION_MAP.get(kalshi_city.upper())
    if not station:
        return None

    rows = get_recent_pressure(station, days=4)
    if len(rows) < 4:
        return None

    # rows are DESC (most recent first)
    # rows[0] = yesterday, rows[3] = 3 days ago
    p_yesterday = rows[0][1]
    p_3_days_ago = rows[3][1]

    if p_yesterday is None or p_3_days_ago is None:
        return None

    dp = p_yesterday - p_3_days_ago

    if abs(dp) < PRESSURE_THRESHOLD:
        return None  # no signal — skip trade

    direction = 'up' if dp > 0 else 'down'
    return (direction, dp)


if __name__ == "__main__":
    # Quick demo
    for city in sorted(KALSHI_STATION_MAP.keys()):
        signal = get_signal(city)
        if signal:
            print(f"{city:>6} → {signal[0].upper():>4}  (ΔP={signal[1]:+.2f}mb)")
        else:
            print(f"{city:>6} →  —    (no signal)")
