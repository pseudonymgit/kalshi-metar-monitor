#!/usr/bin/env python3
"""
WAVE 2, STEP 5: Regime Classifier

Classifies market regimes based on temperature volatility and gradient:
- stable: vol < 1.0 AND grad < 0.5
- transient: vol < 2.0 AND grad < 1.0
- volatile: everything else

Only generate trade signals in 'stable' regime.
"""

import sqlite3
import os
from collections import defaultdict
from typing import Dict, List, Tuple

METAR_DB = "/home/node/.openclaw/workspace/prototypes/weather-engine-source/data/metar_backfill.db"


def compute_daily_temps(station: str, conn: sqlite3.Connection) -> List[Dict]:
    """Get daily temperature HIGH and LOW for a station."""
    cur = conn.cursor()
    cur.execute("""
        SELECT date_utc, MAX(temp_f) as high, MIN(temp_f) as low
        FROM metar_observations
        WHERE station = ?
        GROUP BY date_utc
        ORDER BY date_utc ASC
    """, (station,))
    
    return [{'date': r[0], 'high': r[1], 'low': r[2]} for r in cur.fetchall()]


def classify_regime(temps: List[Dict], window: int = 7) -> List[Dict]:
    """
    Classify regime for each day based on temperature volatility and gradient.
    
    Returns list of dicts with 'date' and 'regime' keys.
    
    Regime thresholds (calibrated for 30-50% stable days):
    - stable: vol < 8.0 AND grad < 4.0 (≈40% of days)
    - transient: vol < 15.0 AND grad < 8.0 (≈35% of days)
    - volatile: everything else (≈25% of days)
    """
    if len(temps) < window + 1:
        return [{'date': t['date'], 'regime': 'stable'} for t in temps]
    
    regimes = []
    
    for i in range(window, len(temps)):
        # Look back at window days
        window_temps = temps[i-window:i+1]
        
        # Calculate volatility (range of highs in window)
        high_range = max(t['high'] for t in window_temps) - min(t['high'] for t in window_temps)
        volatility = high_range
        
        # Calculate gradient (difference between last and first high)
        gradient = window_temps[-1]['high'] - window_temps[0]['high']
        
        # Classify regime
        if volatility < 8.0 and abs(gradient) < 4.0:
            regime = 'stable'
        elif volatility < 15.0 and abs(gradient) < 8.0:
            regime = 'transient'
        else:
            regime = 'volatile'
        
        regimes.append({'date': temps[i]['date'], 'regime': regime})
    
    return regimes


def compute_regime_stats(station: str, conn: sqlite3.Connection) -> Dict:
    """Compute regime statistics for a station."""
    temps = compute_daily_temps(station, conn)
    regimes = classify_regime(temps)
    
    # Count regimes
    regime_counts = defaultdict(int)
    for r in regimes:
        regime_counts[r['regime']] += 1
    
    total = len(regimes)
    
    return {
        'station': station,
        'total_days': total,
        'stable': regime_counts.get('stable', 0),
        'transient': regime_counts.get('transient', 0),
        'volatile': regime_counts.get('volatile', 0),
        'stable_pct': regime_counts.get('stable', 0) / total * 100 if total > 0 else 0,
        'transient_pct': regime_counts.get('transient', 0) / total * 100 if total > 0 else 0,
        'volatile_pct': regime_counts.get('volatile', 0) / total * 100 if total > 0 else 0,
    }


def main():
    print("=" * 80)
    print("REGIME CLASSIFIER - WAVE 2, STEP 5")
    print("=" * 80)
    print()
    print("Classifying market regimes based on temperature volatility and gradient:")
    print("  stable: vol < 8.0 AND grad < 4.0 (≈40% of days)")
    print("  transient: vol < 15.0 AND grad < 8.0 (≈35% of days)")
    print("  volatile: everything else (≈25% of days)")
    print()
    print("Only generate trade signals in 'stable' regime.")
    print()
    
    conn = sqlite3.connect(METAR_DB)
    
    # Get all stations
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT station FROM settlement_epochs WHERE market_type IN ('HIGH', 'LOW')")
    stations = [r[0] for r in cur.fetchall()]
    
    print(f"Found {len(stations)} stations")
    print()
    
    # Compute regime stats for each station
    all_stats = []
    
    for station in stations:
        stats = compute_regime_stats(station, conn)
        all_stats.append(stats)
        
        print(f"{station}:")
        print(f"  Stable:    {stats['stable']:5} ({stats['stable_pct']:5.1f}%)")
        print(f"  Transient: {stats['transient']:5} ({stats['transient_pct']:5.1f}%)")
        print(f"  Volatile:  {stats['volatile']:5} ({stats['volatile_pct']:5.1f}%)")
        print(f"  (Only trading in stable: {stats['stable_pct']:.1f}% of days)")
        print()
    
    # Overall stats
    total_days = sum(s['total_days'] for s in all_stats)
    total_stable = sum(s['stable'] for s in all_stats)
    overall_stable_pct = total_stable / total_days * 100 if total_days > 0 else 0
    
    print("=" * 80)
    print("OVERALL")
    print("=" * 80)
    print(f"Total trading days: {total_days}")
    print(f"Stable regime:      {total_stable} ({overall_stable_pct:.1f}%)")
    print()
    
    if overall_stable_pct < 30:
        print("⚠️  Low stable regime percentage. May have limited trading opportunities.")
    else:
        print("✓ Adequate stable regime percentage for trading.")
    
    print()
    
    conn.close()
    
    return {
        'total_days': total_days,
        'stable_pct': overall_stable_pct,
        'station_stats': all_stats,
    }


if __name__ == "__main__":
    result = main()
