#!/usr/bin/env python3
"""Bounded, resumable GEFS operational refresh.

This is a deterministic script runner, not an agent task. It runs one NOAA
GEFS init cycle and one forecast lead, then promotes only rows explicitly
labeled ``gefs_ens`` into the local archive. It writes a JSON receipt on every
exit so a supervisor can distinguish started/completed/failed/stale.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
RECEIPT = DATA / "gefs_refresh_receipt.json"
LOCK = DATA / ".gefs_refresh.lock"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def write_receipt(payload: dict) -> None:
    payload = {"updated_at": utcnow().isoformat(), **payload}
    tmp = RECEIPT.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    tmp.replace(RECEIPT)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeout", type=int, default=1200)
    ap.add_argument("--max-members", type=int, default=31)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if LOCK.exists():
        try:
            age = time.time() - LOCK.stat().st_mtime
        except OSError:
            age = 0
        if age < args.timeout + 300:
            write_receipt({"status": "skipped", "reason": "lock_exists", "lock_age_seconds": round(age)})
            return 2
        LOCK.unlink(missing_ok=True)

    LOCK.write_text(str(os.getpid()))
    started = utcnow()
    write_receipt({"status": "started", "pid": os.getpid(), "source": "noaa_gefs_operational", "init_date": (started.date() - timedelta(days=1)).isoformat(), "lead_hours": 24})

    init_date = (started.date() - timedelta(days=1)).isoformat()
    cmd = [sys.executable, str(ROOT / "scripts" / "gefs_reforecast_backfill.py"),
           "--source", "operational", "--start-date", init_date,
           "--end-date", init_date, "--steps", "24",
           "--max-members", str(args.max_members), "--delay", "0.5"]
    if args.dry_run:
        cmd.append("--dry-run")

    try:
        proc = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True, timeout=args.timeout)
        output = (proc.stdout + "\n" + proc.stderr)[-12000:]
        status = "completed" if proc.returncode == 0 else "failed"
        write_receipt({"status": status, "returncode": proc.returncode, "command": cmd, "output_tail": output})
        return proc.returncode
    except subprocess.TimeoutExpired as exc:
        write_receipt({"status": "failed", "reason": "timeout", "timeout_seconds": args.timeout, "output_tail": str(exc)})
        return 124
    finally:
        LOCK.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
