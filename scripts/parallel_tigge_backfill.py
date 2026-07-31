#!/usr/bin/env python3
"""
parallel_tigge_backfill.py — Parallel ECMWF TIGGE 51-member backfill.

Splits the backfill into month-sized chunks and processes them in parallel
using subprocess workers. Each worker handles one month for all stations.

Usage:
    python3 scripts/parallel_tigge_backfill.py                          # Full backfill
    python3 scripts/parallel_tigge_backfill.py --workers 4             # 4 parallel workers
    python3 scripts/parallel_tigge_backfill.py --start 2020-01 --end 2020-06  # 6 months
    python3 scripts/parallel_tigge_backfill.py --stations KNYC,KLAX    # 2 stations

B-Mode compliant. No AI/ML.
"""

import os
import sys
import time
import json
import sqlite3
import subprocess
import multiprocessing
from datetime import datetime, timezone, date, timedelta
from pathlib import Path
from typing import List, Tuple

# ── Paths ──
REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = REPO_ROOT / "scripts"
DB_PATH = REPO_ROOT / "data" / "tigge_archive.db"

# ── Constants ──
STATIONS = [
    "KATL", "KAUS", "KBOS", "KDCA", "KDEN", "KDFW", "KHOU", "KLAS",
    "KLAX", "KMDW", "KMIA", "KMSP", "KMSY", "KNYC", "KOKC", "KPHL",
    "KPHX", "KSAT", "KSEA", "KSFO",
]

DEFAULT_START = "2020-01"
DEFAULT_END = "2026-06"


def generate_month_chunks(start_ym: str, end_ym: str) -> List[Tuple[str, str]]:
    """Generate monthly chunks from start to end YYYY-MM."""
    start_parts = start_ym.split("-")
    end_parts = end_ym.split("-")
    s_y, s_m = int(start_parts[0]), int(start_parts[1])
    e_y, e_m = int(end_parts[0]), int(end_parts[1])
    
    chunks = []
    y, m = s_y, s_m
    while (y < e_y) or (y == e_y and m <= e_m):
        chunks.append(f"{y}-{m:02d}")
        if m == 12:
            y += 1
            m = 1
        else:
            m += 1
    
    return chunks


def run_tigge_month(month_key: str, stations: List[str], db_path: str, timeout_s: int = 600) -> dict:
    """Run tigge_backfill.py for a single month and return results."""
    cmd = [
        sys.executable,
        str(SCRIPT_DIR / "tigge_backfill.py"),
        "--start", month_key,
        "--end", month_key,
        "--stations", ",".join(stations),
        "--db-path", db_path,
        "--request-delay", "2",
    ]
    
    result = {"month": month_key, "status": "pending", "rows": 0, "error": None}
    
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            cwd=str(REPO_ROOT),
        )
        
        if proc.returncode == 0:
            result["status"] = "success"
            # Parse output for row count
            for line in proc.stdout.split("\n"):
                if "DB:" in line and "rows" in line:
                    try:
                        parts = line.split()
                        for i, p in enumerate(parts):
                            if p == "rows":
                                result["rows"] = int(parts[i-1])
                    except (ValueError, IndexError):
                        pass
        else:
            result["status"] = "failed"
            result["error"] = proc.stderr[-500:] if proc.stderr else "unknown error"
    except subprocess.TimeoutExpired:
        result["status"] = "timeout"
        result["error"] = f"exceeded {timeout_s}s timeout"
    except Exception as e:
        result["status"] = "error"
        result["error"] = str(e)
    
    return result


def worker_task(args):
    """Worker function for multiprocessing pool."""
    month_key, stations, db_path, timeout = args
    return run_tigge_month(month_key, stations, db_path, timeout)


def run_parallel_backfill(
    start_ym: str = DEFAULT_START,
    end_ym: str = DEFAULT_END,
    stations: List[str] = None,
    n_workers: int = 2,
):
    """Run the TIGGE backfill in parallel using multiprocessing."""
    if stations is None:
        stations = STATIONS
    
    # Generate all month chunks
    months = generate_month_chunks(start_ym, end_ym)
    print(f"TIGGE Parallel Backfill")
    print(f"  Months: {months[0]} to {months[-1]} ({len(months)} months)")
    print(f"  Stations: {len(stations)}")
    print(f"  Workers: {n_workers}")
    print(f"  DB: {DB_PATH}")
    print("=" * 60)
    
    # Check which months are already done
    conn = sqlite3.connect(str(DB_PATH))
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT year_month FROM tigge_checkpoint WHERE status='done'")
    done_months = set(r[0] for r in cur.fetchall())
    conn.close()
    
    pending = [m for m in months if m not in done_months]
    print(f"  Already done: {len(done_months)} months")
    print(f"  Pending: {len(pending)} months")
    print("=" * 60)
    
    if not pending:
        print("All months complete!")
        return
    
    # Prepare worker args
    worker_args = [(m, stations, str(DB_PATH), 600) for m in pending]
    
    # Run in parallel
    t_start = time.time()
    results = []
    
    with multiprocessing.Pool(processes=n_workers) as pool:
        for result in pool.imap_unordered(worker_task, worker_args):
            results.append(result)
            elapsed = time.time() - t_start
            done = len(results)
            total = len(pending)
            rate = done / (elapsed / 60) if elapsed > 0 else 0
            remaining = (total - done) / rate if rate > 0 else 0
            
            status_icon = "✓" if result["status"] == "success" else "✗"
            print(f"  [{done}/{total}] {status_icon} {result['month']}: "
                  f"{result['status']} ({result['rows']} rows) "
                  f"| {elapsed/60:.1f}min elapsed, ~{remaining:.0f}min remaining")
    
    # Summary
    elapsed = time.time() - t_start
    successes = sum(1 for r in results if r["status"] == "success")
    failures = sum(1 for r in results if r["status"] != "success")
    total_rows = sum(r["rows"] for r in results if r["status"] == "success")
    
    print("\n" + "=" * 60)
    print(f"  BACKFILL COMPLETE")
    print(f"  Successes: {successes}/{len(results)}")
    print(f"  Failures:  {failures}")
    print(f"  Total rows: {total_rows}")
    print(f"  Time: {elapsed:.1f}s ({elapsed/60:.1f} min)")
    print("=" * 60)
    
    # Report failures
    if failures > 0:
        print("\nFAILURES:")
        for r in results:
            if r["status"] != "success":
                print(f"  {r['month']}: {r['error']}")
    
    return results


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Parallel TIGGE ECMWF backfill")
    parser.add_argument("--start", default=DEFAULT_START, help=f"Start YYYY-MM (default: {DEFAULT_START})")
    parser.add_argument("--end", default=DEFAULT_END, help=f"End YYYY-MM (default: {DEFAULT_END})")
    parser.add_argument("--stations", type=str, help="Comma-separated stations")
    parser.add_argument("--workers", type=int, default=2, help="Parallel workers (default: 2)")
    args = parser.parse_args()
    
    stations = None
    if args.stations:
        stations = [s.strip().upper() for s in args.stations.split(",")]
    
    run_parallel_backfill(
        start_ym=args.start,
        end_ym=args.end,
        stations=stations,
        n_workers=args.workers,
    )