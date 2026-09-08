#!/usr/bin/env python3
"""
Intraday Daemon — Weather Engine Production Cycle

Cycles the hourly pipeline (L0-L3) at configurable interval.
Entry decisions only when --live-entries is passed.

Usage:
    python3 scripts/intraday_daemon.py --interval-min 5              # Dry run
    python3 scripts/intraday_daemon.py --interval-min 5 --live-entries  # Paper trades
    python3 scripts/intraday_daemon.py --once                          # Single cycle
"""

import argparse
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    force=True,
)
logger = logging.getLogger(__name__)

PID_FILE = "/tmp/intraday-daemon.pid"
STATE_FILE = "/tmp/intraday-daemon-state.json"
_STATE = {}

def _write_pid():
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))
    logger.info(f"PID file written: {os.getpid()} -> {PID_FILE}")

def _remove_pid():
    if os.path.exists(PID_FILE):
        os.remove(PID_FILE)
        logger.info("PID file removed")

def _save_state(**kwargs):
    _STATE.update(kwargs)
    _STATE["last_updated"] = datetime.now(timezone.utc).isoformat()
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(_STATE, f)
    except Exception as e:
        logger.warning(f"State save failed: {e}")

def _check_pid():
    if os.path.exists(PID_FILE):
        try:
            with open(PID_FILE) as f:
                old_pid = int(f.read().strip())
            if os.path.exists(f"/proc/{old_pid}"):
                logger.error(f"Daemon already running (pid {old_pid}, per {PID_FILE}) — exiting")
                sys.exit(1)
            else:
                logger.warning(f"Stale PID file {PID_FILE} (pid {old_pid} dead) — overwriting")
        except (ValueError, FileNotFoundError):
            pass
    _write_pid()

def run(interval_min: int, dry_run: bool, once: bool, force: bool):
    """Main daemon loop."""
    _check_pid()
    _save_state(dry_run=dry_run, interval_min=interval_min, pid=os.getpid())
    
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    
    logger.info(f"Daemon starting: interval={interval_min}min dry_run={dry_run} mode={'24x7' if not once else 'once'}")
    
    consecutive_failures = 0
    max_failures = 10
    
    while True:
        cycle_start = time.time()
        try:
            from scripts.intraday_runner import run_intraday_cycle
            result = run_intraday_cycle(dry_run=dry_run)
            
            if isinstance(result, dict) and "error" in result:
                consecutive_failures += 1
                logger.error(f"Cycle error: {result['error']} ({consecutive_failures}/{max_failures})")
            else:
                consecutive_failures = 0
            
            _save_state(
                last_cycle=datetime.now(timezone.utc).isoformat(),
                consecutive_failures=consecutive_failures,
                status="running",
            )
            
            if consecutive_failures >= max_failures:
                logger.critical(f"{max_failures} consecutive failures — halting daemon")
                break
                
        except Exception as e:
            consecutive_failures += 1
            logger.error(f"Cycle crashed: {e}", exc_info=True)
            _save_state(consecutive_failures=consecutive_failures)
            if consecutive_failures >= max_failures:
                logger.critical(f"{max_failures} consecutive failures — halting daemon")
                break
        
        cycle_time = time.time() - cycle_start
        logger.debug(f"Cycle complete: {cycle_time:.1f}s")
        
        if once:
            logger.info("--once complete — exiting")
            break
        
        sleep_seconds = max(1, interval_min * 60 - cycle_time)
        time.sleep(sleep_seconds)
    
    _save_state(status="stopped")
    _remove_pid()

def _handle_signal(signum, frame):
    sig_name = signal.Signals(signum).name
    logger.info(f"Received {sig_name} — shutting down cleanly")
    _save_state(status="stopped", signal=sig_name)
    _remove_pid()
    sys.exit(0)

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Intraday Weather Engine Daemon")
    ap.add_argument("--interval-min", type=int, default=5, help="Cycle interval in minutes")
    ap.add_argument("--once", action="store_true", help="Run one cycle and exit")
    ap.add_argument("--force", action="store_true", help="Force run even if outside window")
    ap.add_argument("--live-entries", action="store_true", help="Allow entry decisions")
    args = ap.parse_args()
    
    run(
        interval_min=args.interval_min,
        dry_run=not args.live_entries,
        once=args.once,
        force=args.force,
    )