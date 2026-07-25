#!/usr/bin/env python3
"""
halt_check.py — Halt File Mechanism (v1.0 — 2026-07-23)

Checks for a halt signal file at data/.halt before engine startup.
If the halt file exists, logs "HALTED" and exits with code 0 (graceful skip).
Any abort condition should write the halt file to prevent further execution.

Usage:
    python3 scripts/halt_check.py
    python3 scripts/halt_check.py --write "reason: database corruption detected"
    python3 scripts/halt_check.py --clear
    python3 scripts/halt_check.py --status

Exit codes:
    0 = halt file present (or operation succeeded)
    1 = halt file not present (normal operation)
    2 = runtime error
"""

import os
import sys
import argparse
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
HALT_FILE = REPO_ROOT / "data" / ".halt"


def halt_file_exists() -> bool:
    """Check if halt file exists."""
    return HALT_FILE.exists()


def read_halt_reason() -> str:
    """Read the halt reason from the halt file."""
    if not halt_file_exists():
        return ""
    try:
        return HALT_FILE.read_text().strip()
    except Exception:
        return "unknown"


def write_halt(reason: str = "Manual halt") -> bool:
    """Write halt file with a reason string."""
    try:
        HALT_FILE.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        content = f"HALTED\nTimestamp: {ts}\nReason: {reason}\n"
        HALT_FILE.write_text(content)
        return True
    except Exception as e:
        print(f"[ERROR] Failed to write halt file: {e}", file=sys.stderr)
        return False


def clear_halt() -> bool:
    """Remove the halt file."""
    if not halt_file_exists():
        return True
    try:
        HALT_FILE.unlink()
        return True
    except Exception as e:
        print(f"[ERROR] Failed to clear halt file: {e}", file=sys.stderr)
        return False


def main():
    parser = argparse.ArgumentParser(
        description="Check for halt signal file before engine startup"
    )
    parser.add_argument(
        "--write",
        type=str,
        help="Write halt file with the given reason string",
        default=None,
    )
    parser.add_argument("--clear", action="store_true", help="Clear the halt file")
    parser.add_argument("--status", action="store_true", help="Check halt status only")
    args = parser.parse_args()

    if args.write:
        success = write_halt(args.write)
        if success:
            print(f"[HALT] Halt file written: {args.write}")
            sys.exit(0)
        else:
            sys.exit(2)

    if args.clear:
        success = clear_halt()
        if success:
            print("[HALT] Halt file cleared")
            sys.exit(0)
        else:
            sys.exit(2)

    if args.status:
        if halt_file_exists():
            reason = read_halt_reason()
            print(f"[HALT] Halt file PRESENT: {reason}")
            sys.exit(0)
        else:
            print("[HALT] Halt file absent — normal operation")
            sys.exit(1)

    # Default: check mode for engine startup
    if halt_file_exists():
        reason = read_halt_reason()
        print(f"[HALT] HALTED — {reason}")
        sys.exit(0)
    else:
        print("[HALT] No halt file — proceeding normally")
        sys.exit(1)


if __name__ == "__main__":
    main()