#!/usr/bin/env python3
"""
Migration: Add init_cycle column to gefs_archive for epoch-based Kelly (P1.4).

GEFS model cycles: 00Z, 06Z, 12Z, 18Z. Existing backfilled rows lack cycle
information; default '12Z' (most stable cycle, used for backtest assumption).
Going forward, the live NWP collection path should stamp the cycle based on
the forecast fetch time.

Usage:
    python3 scripts/migrate_gefs_cycle.py

This is idempotent (column created only if missing).
"""

import sqlite3
import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
GEFS_DB = os.path.join(REPO_ROOT, "data", "gefs_archive.db")

def main():
    conn = sqlite3.connect(GEFS_DB)
    cur = conn.cursor()
    
    # Check if column already exists
    cur.execute("PRAGMA table_info(gefs_archive)")
    cols = [r[1] for r in cur.fetchall()]
    
    if "init_cycle" not in cols:
        cur.execute("ALTER TABLE gefs_archive ADD COLUMN init_cycle TEXT DEFAULT '12Z'")
        conn.commit()
        print("✅ Added init_cycle column to gefs_archive (default '12Z')")
    else:
        print("ℹ️  init_cycle column already exists")
    
    # Verify
    cur.execute("SELECT init_cycle, COUNT(*) FROM gefs_archive GROUP BY init_cycle")
    print(f"  Distribution: {cur.fetchall()}")
    print(f"  Column added: {os.path.getsize(GEFS_DB)/1e6:.1f} MB")
    
    conn.close()

if __name__ == "__main__":
    main()
