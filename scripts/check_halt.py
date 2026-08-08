#!/usr/bin/env python3
"""
Halt File Check — Maintenance gate for the weather engine trading pipeline.

Creates, checks, and removes the halt file at /tmp/halt_weather_engine.

When the halt file exists, the paper trading cron should:
- NOT place new trades
- Close any open positions (sell at market)
- Log the halt reason
- Wait until the halt file is removed

Usage:
    python3 scripts/check_halt.py        # Check if halted (exit 0=OK, 1=halted)
    python3 scripts/check_halt.py --halt "Upgrading database"   # Create halt
    python3 scripts/check_halt.py --resume    # Remove halt

B-Mode compliant. No AI/ML.
"""
import os
import sys
import argparse
from datetime import datetime, timezone

HALT_FILE = "/tmp/halt_weather_engine"


def halt(reason: str):
    """Create halt file with reason and timestamp."""
    with open(HALT_FILE, "w") as f:
        f.write(f"halting_weather_engine\n")
        f.write(f"reason: {reason}\n")
        f.write(f"halted_at: {datetime.now(timezone.utc).isoformat()}\n")
    print(f"⚠️  Engine HALTED: {reason}")
    return 0


def resume():
    """Remove halt file."""
    if os.path.exists(HALT_FILE):
        os.remove(HALT_FILE)
        print("✅ Engine RESUMED — halt file removed")
    else:
        print("ℹ️  No halt file — engine already running")
    return 0


def check():
    """Check if halt file exists."""
    if os.path.exists(HALT_FILE):
        with open(HALT_FILE) as f:
            content = f.read()
        print(f"⛔ ENGINE HALTED")
        print(content)
        return 1
    else:
        print("✅ Engine RUNNING — no halt file")
        return 0


def main():
    parser = argparse.ArgumentParser(description="Weather engine halt file management")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--halt", type=str, nargs="?", const="Manual halt", default=None,
                        help="Halt the engine with an optional reason")
    group.add_argument("--resume", action="store_true", help="Resume the engine")
    args = parser.parse_args()

    if args.halt is not None:
        return halt(args.halt)
    elif args.resume:
        return resume()
    else:
        return check()


if __name__ == "__main__":
    sys.exit(main())