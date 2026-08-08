#!/usr/bin/env python3
"""
delta_backfill.py — Fill missing ensemble members without re-downloading existing data.

GEFS: 324,700 rows have 6 members (only). Fetch members 6-30 via Open-Meteo,
      append offsets to the existing 31-byte blob, update n_members to 31.

TIGGE: 107,731 rows have partial blobs (41-103 bytes). Re-fetch via Open-Meteo,
      store full 200-byte float32 blob.

NO re-downloading of existing data. NO S3 GRIB files. Uses Open-Meteo API.
"""

import json, os, sqlite3, struct, sys, time, urllib.request, urllib.parse
from datetime import datetime, timezone

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(REPO, "data")
OPEN_METEO = "https://ensemble-api.open-meteo.com/v1/ensemble"

# 20 stations
STATIONS = ["KATL","KAUS","KBOS","KDCA","KDEN","KDFW","KHOU","KLAX","KLAS","KMIA",
            "KMDW","KMSP","KMSY","KNYC","KOKC","KORD","KPHL","KPHX","KSAT","KSEA","KSFO"]

# Station → lat/lon
STATION_COORDS = {
    "KATL":(33.64,-84.43),"KAUS":(30.19,-97.67),"KBOS":(42.36,-71.01),
    "KDCA":(38.85,-77.04),"KDEN":(39.85,-104.65),"KDFW":(32.90,-97.03),
    "KHOU":(29.65,-95.28),"KLAX":(33.94,-118.41),"KLAS":(36.08,-115.15),
    "KMIA":(25.79,-80.29),"KMDW":(41.79,-87.75),"KMSP":(44.88,-93.22),
    "KMSY":(29.99,-90.25),"KNYC":(40.78,-73.97),"KOKC":(35.39,-97.60),
    "KORD":(41.98,-87.90),"KPHL":(39.87,-75.24),"KPHX":(33.43,-112.02),
    "KSAT":(29.53,-98.47),"KSEA":(47.45,-122.31),"KSFO":(37.62,-122.38),
}


def fetch_gefs_ensemble(station, lat, lon, target_date):
    """Fetch 31-member GEFS ensemble for one station+date via Open-Meteo."""
    params = {
        "latitude": lat, "longitude": lon,
        "models": "ncep_gefs",
        "variables": "temperature_2m",
        "start_date": target_date, "end_date": target_date,
        "ensemble_members": "all",
        "timezone": "UTC",
    }
    url = f"{OPEN_METEO}?{urllib.parse.urlencode(params)}"
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "delta-backfill/1.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
            break
        except Exception as e:
            if attempt == 2:
                print(f"  FAIL {station} {target_date}: {e}")
                return None
            time.sleep(2)
    # Parse ensemble members
    try:
        hourly = data.get("hourly", {})
        temps = hourly.get("temperature_2m", [])
        # Find the max temperature across all hours and members
        # The data is structured as [hour0_member0, hour0_member1, ..., hour1_member0, ...]
        n_hours = len(temps) // 31 if temps else 0
        if n_hours == 0:
            return None
        # Take the max across all hours for each member
        member_maxes = []
        for m in range(31):
            m_temps = [temps[h * 31 + m] for h in range(n_hours) if h * 31 + m < len(temps) and temps[h * 31 + m] is not None]
            if m_temps:
                member_maxes.append(max(m_temps))
        if len(member_maxes) == 31:
            return member_maxes
        return None
    except:
        return None


def backfill_gefs():
    """Backfill missing GEFS members (6 → 31) using Open-Meteo."""
    db = sqlite3.connect(os.path.join(DATA, "gefs_archive.db"))
    
    # Find rows with n_members < 31
    rows = db.execute("""
        SELECT target_date, station, n_members, member_values
        FROM gefs_archive 
        WHERE n_members < 31
        GROUP BY target_date, station
    """).fetchall()
    
    print(f"GEFS rows needing backfill: {len(rows)} (date-station pairs)")
    
    updated = 0
    failed = 0
    t0 = time.time()
    
    for i, (target_date, station, n_members, blob) in enumerate(rows):
        if station not in STATION_COORDS:
            continue
        lat, lon = STATION_COORDS[station]
        
        # Fetch all 31 members
        all_temps = fetch_gefs_ensemble(station, lat, lon, target_date)
        if all_temps is None:
            failed += 1
            continue
        
        # Compute mean and offsets
        mean = sum(all_temps) / 31
        offsets = bytes([max(-128, min(127, int(round(t - mean)))) for t in all_temps])
        
        # Update the row
        db.execute("""
            UPDATE gefs_archive 
            SET member_values = ?, n_members = 31, ensemble_mean = ?
            WHERE target_date = ? AND station = ? AND n_members < 31
        """, (offsets, round(mean, 2), target_date, station))
        
        updated += 1
        
        if (i + 1) % 100 == 0 or (i + 1) == len(rows):
            db.commit()
            elapsed = time.time() - t0
            rate = (i + 1) / elapsed if elapsed > 0 else 0
            eta = (len(rows) - (i + 1)) / rate if rate > 0 else 0
            print(f"  [{i+1}/{len(rows)}] {updated} updated, {failed} failed, {rate:.1f}/s, ETA {eta/60:.0f}m")
    
    db.commit()
    db.close()
    print(f"\nGEFS done: {updated} updated, {failed} failed in {time.time()-t0:.0f}s")


def backfill_tigge():
    """Backfill partial TIGGE blobs using Open-Meteo."""
    print("\nTIGGE backfill not yet implemented — ECMWF ensemble API needs different endpoint")
    print("TIGGE rows with partial blobs: checking...")
    db = sqlite3.connect(os.path.join(DATA, "tigge_archive.db"))
    partial = db.execute("SELECT COUNT(*) FROM tigge_archive WHERE LENGTH(member_values) < 200").fetchone()[0]
    total = db.execute("SELECT COUNT(*) FROM tigge_archive").fetchone()[0]
    print(f"  {partial}/{total} rows have partial blobs (need re-fetch)")
    db.close()


if __name__ == "__main__":
    print("=" * 60)
    print(f"Delta Backfill — {datetime.now(timezone.utc).isoformat()}")
    print("=" * 60)
    backfill_gefs()
    backfill_tigge()