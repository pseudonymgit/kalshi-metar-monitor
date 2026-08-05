#!/usr/bin/env python3
"""
Populate gefs_archive.db with recent GEFS data from nwp_forecasts.db.
Converts flat NWP records to gefs_archive blob format (int8 offsets from mean).

Usage:
    python3 scripts/populate_gefs_archive.py  # fills all missing dates
    python3 scripts/populate_gefs_archive.py --dates 2026-07-31 2026-08-01 2026-08-02 2026-08-03 2026-08-04
"""

import sqlite3, struct, sys, os
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
GEFS_DB = str(REPO_ROOT / "data" / "gefs_archive.db")
NWP_DB = str(REPO_ROOT / "data" / "nwp_forecasts.db")

def get_missing_dates(gefs_conn):
    """Get dates that exist in NWP gefs_ens but are missing from gefs_archive step=24."""
    cur = gefs_conn.execute("SELECT MAX(target_date) FROM gefs_archive WHERE step=24")
    max_date = cur.fetchone()[0]
    if not max_date:
        return []
    from datetime import date, timedelta
    today = date.today().isoformat()
    missing = []
    d = date.fromisoformat(max_date)
    d += timedelta(days=1)
    while d.isoformat() <= today:
        missing.append(d.isoformat())
        d += timedelta(days=1)
    return missing

def get_nwp_data(target_date):
    """Get GEFS ensemble data from nwp_forecasts.db for a target_date."""
    nwp = sqlite3.connect(NWP_DB)
    cur = nwp.execute("""
        SELECT station, value FROM nwp_forecasts
        WHERE model='gefs_ens' AND target_date = ?
        ORDER BY station, variable
    """, (target_date,))
    rows = cur.fetchall()
    nwp.close()
    
    stations = {}
    for stn, val in rows:
        if stn not in stations:
            stations[stn] = []
        stations[stn].append(val)
    return stations

def encode_blob(member_temps_c, mean_c):
    """Encode member temps as int8 offsets from mean (*10) for gefs_archive format."""
    offsets = bytearray()
    for t in member_temps_c:
        diff = max(-128, min(127, int(round((t - mean_c) * 10))))
        offsets.append(diff & 0xFF)
    return bytes(offsets)

def fill_date(target_date, gefs_conn):
    """Fill one date's data from NWP to gefs_archive."""
    stations = get_nwp_data(target_date)
    if not stations:
        return 0
    
    count = 0
    now = datetime.utcnow().isoformat()
    for stn, vals in sorted(stations.items()):
        if len(vals) < 3:
            continue
        mean_c = sum(vals) / len(vals)
        min_c = min(vals)
        max_c = max(vals)
        n_members = len(vals)
        blob = encode_blob(vals, mean_c)
        
        for step in [0, 3, 6, 9, 12, 15, 18, 21, 24]:
            try:
                gefs_conn.execute("""
                    INSERT OR REPLACE INTO gefs_archive
                    (target_date, station, step, ensemble_mean, ensemble_min,
                     ensemble_max, member_values, n_members, fetch_timestamp, init_cycle)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '12Z')
                """, (target_date, stn, step, round(mean_c, 2), round(min_c, 2),
                      round(max_c, 2), blob, n_members, now))
                count += 1
            except Exception as e:
                print(f"  ERROR {target_date} {stn} step={step}: {e}")
    
    gefs_conn.commit()
    return count

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--dates", nargs="+", help="Dates to fill (YYYY-MM-DD)")
    args = parser.parse_args()
    
    if not os.path.exists(NWP_DB):
        print(f"ERROR: NWP DB not found at {NWP_DB}")
        return 1
    
    gefs = sqlite3.connect(GEFS_DB)
    
    if args.dates:
        dates_to_fill = args.dates
    else:
        dates_to_fill = get_missing_dates(gefs)
    
    if not dates_to_fill:
        print("No dates to fill — gefs_archive is up to date.")
        return 0
    
    print(f"Filling {len(dates_to_fill)} dates: {dates_to_fill[0]} to {dates_to_fill[-1]}")
    
    total_rows = 0
    for dt in dates_to_fill:
        rows = fill_date(dt, gefs)
        if rows:
            print(f"  {dt}: {rows} rows ({rows//20} stations)")
        else:
            print(f"  {dt}: no NWP data available")
        total_rows += rows
    
    gefs.close()
    
    # Verify
    gefs2 = sqlite3.connect(GEFS_DB)
    cur = gefs2.execute("SELECT MAX(target_date) FROM gefs_archive WHERE step=24")
    print(f"\nDone. New max date in gefs_archive: {cur.fetchone()[0]}")
    cur = gefs2.execute("SELECT COUNT(*) FROM gefs_archive WHERE step=24 AND target_date IN ({})".format(
        ','.join('?' for _ in dates_to_fill)), dates_to_fill)
    filled = cur.fetchone()[0]
    expected = len(dates_to_fill) * 20 * 9  # dates × stations × steps
    print(f"Filled: {filled} rows (expected {expected} if all stations+steps present)")
    gefs2.close()

if __name__ == "__main__":
    sys.exit(main())