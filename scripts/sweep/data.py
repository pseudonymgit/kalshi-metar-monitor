"""
data.py — Load Kalshi settlement data + GEFS ensemble data for sweep evaluation.
"""

import os, sys, sqlite3, json, math
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Tuple, Optional

from . import config

# ── Kalshi Settlement Data Loader ──

def load_kalshi_settlements(
    stations: Optional[List[str]] = None,
    min_date: Optional[str] = None,
    max_date: Optional[str] = None,
    source_type: str = "finalized",
) -> Dict[str, Dict[str, float]]:
    """
    Load Kalshi settlement ground truth temperatures.
    
    Returns {station: {target_date: temp_f}} for all available records.
    """
    conn = sqlite3.connect(config.DB_PATH)
    cur = conn.cursor()
    
    query = "SELECT station, target_date, kalshi_temp FROM kalshi_settlements"
    filters = []
    params = []
    
    if stations:
        filters.append(f"station IN ({','.join('?' for _ in stations)})")
        params.extend(stations)
    if min_date:
        filters.append("target_date >= ?")
        params.append(min_date)
    if max_date:
        filters.append("target_date <= ?")
        params.append(max_date)
    if source_type:
        filters.append("source_type = ?")
        params.append(source_type)
    
    if filters:
        query += " WHERE " + " AND ".join(filters)
    
    query += " ORDER BY target_date"
    
    cur.execute(query, params)
    rows = cur.fetchall()
    conn.close()
    
    result: Dict[str, Dict[str, float]] = defaultdict(dict)
    for row in rows:
        station, date_str, temp = row[0], row[1], row[2]
        if temp is not None:
            result[station][date_str] = temp
    
    return dict(result)


def load_gefs_archive(
    stations: Optional[List[str]] = None,
    min_date: Optional[str] = None,
    max_date: Optional[str] = None,
) -> Dict[str, Dict[str, dict]]:
    """
    Load GEFS ensemble data from gefs_archive.db.
    
    Returns {station: {target_date: {mean, min, max, n_members, step}}}
    Only returns step=24 (forecast for target date).
    """
    conn = sqlite3.connect(config.GEFS_DB_PATH)
    cur = conn.cursor()
    
    query = """
        SELECT target_date, station, ensemble_mean, ensemble_min, ensemble_max, 
               n_members, step
        FROM gefs_archive
        WHERE step = 24
    """
    filters = []
    params = []
    
    if stations:
        filters.append(f"station IN ({','.join('?' for _ in stations)})")
        params.extend(stations)
    if min_date:
        filters.append("target_date >= ?")
        params.append(min_date)
    if max_date:
        filters.append("target_date <= ?")
        params.append(max_date)
    
    if filters:
        query += " AND " + " AND ".join(filters)
    
    query += " ORDER BY target_date"
    
    cur.execute(query, params)
    rows = cur.fetchall()
    conn.close()
    
    result: Dict[str, Dict[str, dict]] = defaultdict(dict)
    for row in rows:
        date_str, station, mean_, min_, max_, n_mem, step = row
        if mean_ is not None:
            result[station][date_str] = {
                "mean": mean_,
                "min": min_,
                "max": max_,
                "n_members": n_mem or 0,
                "step": step,
            }
    
    return dict(result)


def get_date_range() -> Tuple[str, str]:
    """Get the min and max date from the Kalshi settlements DB."""
    conn = sqlite3.connect(config.DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT MIN(target_date), MAX(target_date) FROM kalshi_settlements")
    row = cur.fetchone()
    conn.close()
    return row[0], row[1]


def get_station_summary() -> Dict[str, int]:
    """Get count of records per station."""
    conn = sqlite3.connect(config.DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT station, COUNT(*) FROM kalshi_settlements GROUP BY station")
    rows = cur.fetchall()
    conn.close()
    return dict(rows)


# ── Directional Signal Computation ──

def compute_direction(actual: float, prev: float) -> int:
    """Return +1 (up), -1 (down), or 0 (flat) for directional change."""
    if actual > prev:
        return 1
    elif actual < prev:
        return -1
    return 0


def compute_accuracy_simple(
    predictions: List[Tuple[str, str, int, float]],
    ground_truth: Dict[str, Dict[str, float]],
) -> Dict:
    """
    Compute directional accuracy against Kalshi ground truth.
    
    Args:
        predictions: List of (station, date, predicted_direction, confidence)
        ground_truth: {station: {date: temp_f}}
    
    Returns: {accuracy, n_trades, correct, total}
    """
    correct = 0
    total = 0
    for station, date_str, pred_dir, conf in predictions:
        if station in ground_truth and date_str in ground_truth[station]:
            total += 1
            # For direction, we need the previous day's temp
            # This is simplified — actual implementation needs prev day data
            pass
    
    # Placeholder — full implementation needs previous day temperatures
    return {"accuracy": 0.0, "n_trades": 0, "correct": 0, "total": 0}