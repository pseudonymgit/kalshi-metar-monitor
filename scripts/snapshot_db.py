#!/usr/bin/env python3
"""
Weekly DB Snapshot Script (v1.0 — 2026-07-05)

Creates compressed snapshots of weather engine databases.
Maintains 3 rotating copies per database.

Usage:
    python3 scripts/snapshot_db.py [--data-dir data/] [--keep 3]

Scheduled: Every Sunday at 03:00 UTC via OpenClaw cron.

Version: v1.0 2026-07-05
"""

import os
import sys
import gzip
import shutil
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = REPO_ROOT / "data"
DEFAULT_SNAPSHOT_DIR = DEFAULT_DATA_DIR / "snapshots"
DEFAULT_KEEP = 3

# Databases to snapshot
DB_FILES = [
    "metar_backfill.db",
    "paper_trading_dev.db",
    "paper_trading_prod.db",
    "paper_trading_sbox.db",
    "paper_trading.db",  # Legacy
]


def snapshot_db(db_path: Path, snapshot_dir: Path, keep: int = DEFAULT_KEEP) -> bool:
    """
    Create a compressed snapshot of a SQLite database.
    Uses SQLite VACUUM INTO for consistent snapshot, then gzip compresses.
    
    Returns True on success.
    """
    if not db_path.exists():
        return False
    
    db_name = db_path.stem  # e.g., "metar_backfill"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    
    # Rotate existing snapshots: 001 → 002, 002 → 003, delete 003
    for i in range(keep, 0, -1):
        src = snapshot_dir / f"{db_name}_{i:03d}.db.gz"
        if i == keep:
            if src.exists():
                src.unlink()  # Delete oldest
        else:
            dst = snapshot_dir / f"{db_name}_{i+1:03d}.db.gz"
            if src.exists():
                src.rename(dst)
    
    # Create new snapshot at _001
    snapshot_path = snapshot_dir / f"{db_name}_001.db"
    
    try:
        # Use SQLite VACUUM INTO for consistent snapshot
        conn = sqlite3.connect(str(db_path))
        conn.execute(f"VACUUM INTO '{snapshot_path}'")
        conn.close()
        
        # Compress with gzip
        compressed_path = snapshot_dir / f"{db_name}_001.db.gz"
        with open(snapshot_path, 'rb') as f_in:
            with gzip.open(compressed_path, 'wb') as f_out:
                shutil.copyfileobj(f_in, f_out)
        
        # Remove uncompressed version
        snapshot_path.unlink()
        
        # Report size
        size_mb = compressed_path.stat().st_size / (1024 * 1024)
        print(f"  ✓ {db_name}: snapshot created ({size_mb:.1f} MB)")
        return True
        
    except Exception as e:
        print(f"  ✗ {db_name}: snapshot failed — {e}")
        # Clean up partial files
        if snapshot_path.exists():
            snapshot_path.unlink()
        return False


def main():
    import argparse
    
    parser = argparse.ArgumentParser(description="Weekly DB snapshot for weather engine")
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR),
                       help="Data directory containing DBs")
    parser.add_argument("--snapshot-dir", default=str(DEFAULT_SNAPSHOT_DIR),
                       help="Output directory for snapshots")
    parser.add_argument("--keep", type=int, default=DEFAULT_KEEP,
                       help="Number of rotating copies to keep")
    
    args = parser.parse_args()
    
    data_dir = Path(args.data_dir)
    snapshot_dir = Path(args.snapshot_dir)
    
    print(f"\nWeather Engine DB Snapshot")
    print(f"  Data dir: {data_dir}")
    print(f"  Snapshot dir: {snapshot_dir}")
    print(f"  Keep: {args.keep} copies")
    print(f"  Time: {datetime.now(timezone.utc).isoformat()}")
    print()
    
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    
    success_count = 0
    skip_count = 0
    fail_count = 0
    
    for db_file in DB_FILES:
        db_path = data_dir / db_file
        if not db_path.exists():
            print(f"  - {db_file}: not found, skipping")
            skip_count += 1
            continue
        
        if snapshot_db(db_path, snapshot_dir, args.keep):
            success_count += 1
        else:
            fail_count += 1
    
    # Clean up old snapshots that exceed keep count
    for gz_file in snapshot_dir.glob("*.db.gz"):
        # Check if this file's number exceeds keep
        parts = gz_file.stem.split("_")
        if parts:
            try:
                num = int(parts[-1])
                if num > args.keep:
                    gz_file.unlink()
                    print(f"  🗑️ Cleaned up old snapshot: {gz_file.name}")
            except (ValueError, IndexError):
                pass
    
    # Print summary
    total_size = sum(f.stat().st_size for f in snapshot_dir.glob("*.db.gz")) / (1024 * 1024)
    
    print(f"\nSnapshot Summary:")
    print(f"  Created: {success_count}")
    print(f"  Skipped: {skip_count}")
    print(f"  Failed: {fail_count}")
    print(f"  Total snapshot storage: {total_size:.1f} MB")
    print(f"  Snapshot directory: {snapshot_dir}")


if __name__ == "__main__":
    main()
